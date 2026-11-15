// SPDX-License-Identifier: GPL-3.0-only
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    fs::File,
    io::{self, BufRead, BufReader, BufWriter, Read, Write},
    path::Path,
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};

const MAX_BOX: usize = 64 * 1024 * 1024;

struct Fragment {
    moof: Vec<u8>,
    mdat: Vec<u8>,
    timestamp: u64,
    timestamp_offset: usize,
    wide: bool,
    start: f64,
    end: f64,
}
impl Fragment {
    fn bytes(&self) -> usize {
        self.moof.len() + self.mdat.len()
    }

    fn write(&self, output: &mut impl Write, base: u64) -> Result<()> {
        let relative = self
            .timestamp
            .checked_sub(base)
            .context("Fragment timestamps moved backward")?;
        let offset = self.timestamp_offset;
        output.write_all(&self.moof[..offset])?;
        let width = if self.wide {
            output.write_all(&relative.to_be_bytes())?;
            8
        } else {
            output.write_all(&u32::try_from(relative)?.to_be_bytes())?;
            4
        };
        output.write_all(&self.moof[offset + width..])?;
        output.write_all(&self.mdat)?;
        Ok(())
    }
}

fn boxes(mut data: &[u8], mut offset: usize) -> impl Iterator<Item = (usize, &[u8])> {
    std::iter::from_fn(move || {
        let header = data.get(..8)?;
        let size = u32::from_be_bytes(header[..4].try_into().unwrap()) as usize;
        if !(8..=data.len()).contains(&size) {
            return None;
        }
        let result = (offset, &data[..size]);
        data = &data[size..];
        offset += size;
        Some(result)
    })
}

fn timestamp(moof: &[u8]) -> Result<(usize, u64, bool)> {
    for (track_offset, track) in boxes(&moof[8..], 8) {
        if &track[4..8] != b"traf" {
            continue;
        }
        for (offset, field) in boxes(&track[8..], track_offset + 8) {
            if &field[4..8] != b"tfdt" {
                continue;
            }
            let position = offset + 12;
            return match field.get(8) {
                Some(0) if field.len() >= 16 => Ok((
                    position,
                    u32::from_be_bytes(field[12..16].try_into().unwrap()).into(),
                    false,
                )),
                Some(1) if field.len() >= 20 => Ok((
                    position,
                    u64::from_be_bytes(field[12..20].try_into().unwrap()),
                    true,
                )),
                _ => bail!("Invalid MP4 decode timestamp"),
            };
        }
    }
    bail!("MP4 fragment has no decode timestamp")
}

fn fragment_duration(moof: &[u8]) -> Option<u64> {
    let (_, traf) = boxes(moof.get(8..)?, 8).find(|(_, data)| &data[4..8] == b"traf")?;
    let fields = || boxes(&traf[8..], 8);
    let read_u32 = |data: &[u8], offset: usize| {
        Some(u32::from_be_bytes(
            data.get(offset..offset + 4)?.try_into().ok()?,
        ))
    };
    let (_, tfhd) = fields().find(|(_, field)| &field[4..8] == b"tfhd")?;
    let flags = read_u32(tfhd, 8)?;
    let default_offset =
        16 + if flags & 1 != 0 { 8 } else { 0 } + if flags & 2 != 0 { 4 } else { 0 };
    let default_duration = if flags & 8 != 0 {
        read_u32(tfhd, default_offset)
    } else {
        None
    };
    let mut total = 0u64;
    for (_, trun) in fields().filter(|(_, field)| &field[4..8] == b"trun") {
        let flags = read_u32(trun, 8)?;
        let count = read_u32(trun, 12)?;
        if flags & 0x100 == 0 {
            total = total.checked_add(u64::from(count) * u64::from(default_duration?))?;
            continue;
        }
        let offset = 16 + 4 * (flags & 5).count_ones() as usize;
        let sample_size = 4 * (flags & 0xf00).count_ones() as usize;
        let samples = trun.get(offset..)?;
        if count as usize > samples.len() / sample_size {
            return None;
        }
        for sample in samples.chunks_exact(sample_size).take(count as usize) {
            total = total.checked_add(u64::from(read_u32(sample, 0)?))?;
        }
    }
    (total > 0).then_some(total)
}

fn read_box(input: &mut impl Read) -> Result<Option<Vec<u8>>> {
    let mut header = [0; 8];
    if input.read(&mut header[..1])? == 0 {
        return Ok(None);
    }
    input
        .read_exact(&mut header[1..])
        .context("Incomplete MP4 header")?;
    let size = u32::from_be_bytes(header[..4].try_into().unwrap()) as usize;
    ensure!(
        (8..=MAX_BOX).contains(&size),
        "Unsupported MP4 box size {size}"
    );
    let mut data = vec![0; size];
    data[..8].copy_from_slice(&header);
    input
        .read_exact(&mut data[8..])
        .context("Incomplete MP4 payload")?;
    Ok(Some(data))
}

#[derive(Default)]
struct Ring {
    started: Option<f64>,
    init: Vec<Arc<Vec<u8>>>,
    fragments: VecDeque<Arc<Fragment>>,
    error: Option<String>,
}

struct Buffer {
    epoch: Instant,
    paused_at: Option<Instant>,
    seconds: f64,
    limit: usize,
    bytes: usize,
    rings: BTreeMap<u64, Ring>,
}
impl Buffer {
    fn now(&self) -> f64 {
        self.paused_at
            .unwrap_or_else(Instant::now)
            .duration_since(self.epoch)
            .as_secs_f64()
    }

    fn trim(&mut self) {
        let cutoff = self.now() - self.seconds;
        for ring in self.rings.values_mut() {
            let Some(start) = ring.started else { continue };
            while ring
                .fragments
                .front()
                .is_some_and(|f| start + f.end < cutoff)
            {
                self.bytes -= ring.fragments.pop_front().unwrap().bytes();
            }
        }
        while self.bytes > self.limit {
            let Some((_, ring)) = self
                .rings
                .iter_mut()
                .filter(|(_, ring)| !ring.fragments.is_empty())
                .min_by(|(_, a), (_, b)| {
                    let start = |r: &Ring| r.started.unwrap() + r.fragments[0].start;
                    start(a).total_cmp(&start(b))
                })
            else {
                break;
            };
            self.bytes -= ring.fragments.pop_front().unwrap().bytes();
        }
    }
}

fn ingest(
    input: impl Read,
    file: Option<File>,
    state: &Mutex<Buffer>,
    id: u64,
    scale: f64,
    launched: f64,
) -> Result<()> {
    let mut input = BufReader::with_capacity(256 * 1024, input);
    let mut recording = file.map(|file| BufWriter::with_capacity(256 * 1024, file));
    let mut pending = None;
    while let Some(data) = read_box(&mut input)? {
        match &data[4..8] {
            b"ftyp" | b"moov" => {
                if let Some(file) = &mut recording {
                    file.write_all(&data)?;
                }
                state
                    .lock()
                    .unwrap()
                    .rings
                    .get_mut(&id)
                    .unwrap()
                    .init
                    .push(Arc::new(data));
            }
            b"moof" => {
                ensure!(pending.is_none(), "MP4 fragment has no media payload");
                pending = Some(data);
            }
            b"mdat" => {
                let moof = pending.take().context("MP4 media has no fragment header")?;
                let (timestamp_offset, timestamp, wide) = timestamp(&moof)?;
                let start = timestamp as f64 / scale;
                let duration = fragment_duration(&moof).map_or(1.0, |ticks| ticks as f64 / scale);
                if let Some(file) = &mut recording {
                    file.write_all(&moof)?;
                    file.write_all(&data)?;
                }
                let fragment = Fragment {
                    moof,
                    mdat: data,
                    timestamp,
                    timestamp_offset,
                    wide,
                    start,
                    end: start + duration,
                };
                let mut buffer = state.lock().unwrap();
                let now = buffer.now();
                buffer.bytes += fragment.bytes();
                let ring = buffer.rings.get_mut(&id).unwrap();
                ring.started.get_or_insert_with(|| launched.max(now - 1.0));
                ring.fragments.push_back(Arc::new(fragment));
                buffer.trim();
            }
            _ => {}
        }
    }
    ensure!(pending.is_none(), "Incomplete MP4 fragment");
    if let Some(mut file) = recording {
        file.flush()?;
    }
    Ok(())
}

struct Producer {
    child: Child,
    reader: Option<thread::JoinHandle<()>>,
    logger: Option<thread::JoinHandle<()>>,
    log: Arc<Mutex<VecDeque<u8>>>,
    #[cfg(windows)]
    _job: std::os::windows::io::OwnedHandle,
}

#[cfg(windows)]
fn attach_job(child: &Child) -> io::Result<std::os::windows::io::OwnedHandle> {
    use std::{
        os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
        ptr::null,
    };
    use windows_sys::Win32::System::JobObjects::*;
    unsafe {
        let handle = CreateJobObjectW(null(), null());
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        let job = OwnedHandle::from_raw_handle(handle);
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            std::mem::size_of_val(&limits) as u32,
        ) == 0
            || AssignProcessToJobObject(handle, child.as_raw_handle()) == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(job)
    }
}

impl Producer {
    fn start(
        args: &[String],
        file: Option<File>,
        state: Arc<Mutex<Buffer>>,
        id: u64,
        scale: f64,
    ) -> Result<Self> {
        let (program, args) = args.split_first().context("Missing capture command")?;
        let mut command = Command::new(program);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        let launched = state.lock().unwrap().now();
        let mut child = command
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;
        #[cfg(windows)]
        let job = attach_job(&child).inspect_err(|_| {
            let _ = child.kill();
            let _ = child.wait();
        })?;
        let output = child.stdout.take().unwrap();
        let mut stderr = child.stderr.take().unwrap();
        let log = Arc::new(Mutex::new(VecDeque::new()));
        let messages = log.clone();
        let logger = thread::spawn(move || {
            let mut chunk = [0; 4096];
            while let Ok(size @ 1..) = stderr.read(&mut chunk) {
                let mut log = messages.lock().unwrap();
                log.extend(&chunk[..size]);
                let overflow = log.len().saturating_sub(32 * 1024);
                log.drain(..overflow);
            }
        });
        let reader = thread::spawn(move || {
            if let Err(error) = ingest(output, file, &state, id, scale, launched) {
                state.lock().unwrap().rings.get_mut(&id).unwrap().error =
                    Some(format!("{error:#}"));
            }
        });
        Ok(Self {
            child,
            reader: Some(reader),
            logger: Some(logger),
            log,
            #[cfg(windows)]
            _job: job,
        })
    }

    fn failure(&mut self) -> Result<Option<String>> {
        let Some(status) = self.child.try_wait()? else {
            return Ok(None);
        };
        if status.success() {
            return Ok(Some("Capture stopped unexpectedly.".into()));
        }
        let log = self.log.lock().unwrap().iter().copied().collect::<Vec<_>>();
        let log = String::from_utf8_lossy(&log);
        Ok(Some(if log.trim().is_empty() {
            format!("Capture exited with {status}.")
        } else {
            log.trim().into()
        }))
    }

    fn stop(mut self) -> Option<String> {
        let already_exited = self.child.try_wait().ok().flatten().is_some();
        if let Some(mut input) = self.child.stdin.take() {
            let _ = input.write_all(b"q\n");
        }
        let deadline = Instant::now() + Duration::from_secs(8);
        while matches!(self.child.try_wait(), Ok(None)) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        if matches!(self.child.try_wait(), Ok(None)) {
            let _ = self.child.kill();
        }
        let status = self.child.wait();
        #[cfg(windows)]
        drop(self._job);
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
        if let Some(logger) = self.logger.take() {
            let _ = logger.join();
        }
        if already_exited || !status.is_ok_and(|status| status.success()) {
            let log = self.log.lock().unwrap().iter().copied().collect::<Vec<_>>();
            let details = String::from_utf8_lossy(&log);
            return Some(if details.trim().is_empty() {
                "Capture process stopped unexpectedly.".into()
            } else {
                details.trim().into()
            });
        }
        None
    }
}

type MediaSnapshot = (Vec<Arc<Vec<u8>>>, Vec<Arc<Fragment>>);

fn snapshot(path: &Path, (init, fragments): MediaSnapshot) -> Result<()> {
    let mut output = BufWriter::with_capacity(256 * 1024, File::create(path)?);
    for data in init {
        output.write_all(&data)?;
    }
    let base = fragments[0].timestamp;
    for fragment in fragments {
        fragment.write(&mut output, base)?;
    }
    output.flush()?;
    Ok(())
}

fn id_of(request: &Value) -> Result<u64> {
    request["id"].as_u64().context("Missing track ID")
}

fn handle(
    request: Value,
    state: &Arc<Mutex<Buffer>>,
    children: &mut BTreeMap<u64, Producer>,
) -> Result<Value> {
    match request["op"].as_str().unwrap_or_default() {
        "pause_clock" => {
            let mut state = state.lock().unwrap();
            state.paused_at.get_or_insert_with(Instant::now);
            Ok(json!(state.now()))
        }
        "resume_clock" => {
            let mut state = state.lock().unwrap();
            if let Some(started) = state.paused_at.take() {
                state.epoch += started.elapsed();
            }
            Ok(Value::Null)
        }
        "start" => {
            let id = id_of(&request)?;
            let args: Vec<String> = serde_json::from_value(request["command"].clone())?;
            let scale = request["scale"]
                .as_f64()
                .filter(|v| *v > 0.0)
                .context("Invalid timescale")?;
            let file = request["media"].as_str().map(File::create).transpose()?;
            ensure!(
                !state.lock().unwrap().rings.contains_key(&id),
                "Track already exists"
            );
            state.lock().unwrap().rings.insert(id, Ring::default());
            match Producer::start(&args, file, state.clone(), id, scale) {
                Ok(child) => {
                    children.insert(id, child);
                }
                Err(error) => {
                    state.lock().unwrap().rings.remove(&id);
                    return Err(error);
                }
            }
            Ok(Value::Null)
        }
        "status" => {
            let mut state = state.lock().unwrap();
            state.trim();
            Ok(
                json!({"bytes": state.bytes, "tracks": state.rings.iter().map(|(id, ring)| {
                json!({"id": id, "started": ring.started, "count": ring.fragments.len(),
                    "first": ring.fragments.front().map(|f| f.start),
                    "last": ring.fragments.back().map(|f| f.start),
                    "end": ring.fragments.back().map(|f| f.end), "error": ring.error})
            }).collect::<Vec<_>>() }),
            )
        }
        "failure" => {
            let id = id_of(&request)?;
            let error = state
                .lock()
                .unwrap()
                .rings
                .get(&id)
                .and_then(|ring| ring.error.clone());
            if error.is_some() {
                return Ok(json!(error));
            }
            Ok(json!(
                children
                    .get_mut(&id)
                    .map(Producer::failure)
                    .transpose()?
                    .flatten()
            ))
        }
        "stop" => {
            let id = id_of(&request)?;
            let error = children.remove(&id).and_then(Producer::stop);
            let ring_error = state
                .lock()
                .unwrap()
                .rings
                .get(&id)
                .and_then(|ring| ring.error.clone());
            Ok(json!(ring_error.or(error)))
        }
        "forget" => {
            let id = id_of(&request)?;
            ensure!(!children.contains_key(&id), "Track is still running");
            let mut state = state.lock().unwrap();
            if let Some(ring) = state.rings.remove(&id) {
                state.bytes -= ring.fragments.iter().map(|f| f.bytes()).sum::<usize>();
            }
            Ok(Value::Null)
        }
        "snapshot" => {
            let id = id_of(&request)?;
            let from = request["from"].as_f64().context("Missing replay start")?;
            let to = request["to"].as_f64().context("Missing replay end")?;
            let path = request["path"].as_str().context("Missing replay file")?;
            let (first, media) = {
                let state = state.lock().unwrap();
                let ring = state.rings.get(&id).context("Unknown capture track")?;
                let Some(first) = ring.fragments.front() else {
                    return Ok(Value::Null);
                };
                if first.start >= to || ring.fragments.back().unwrap().end <= from {
                    return Ok(Value::Null);
                }
                let start = ring
                    .fragments
                    .iter()
                    .position(|f| f.end > from)
                    .unwrap_or(ring.fragments.len());
                let end = ring
                    .fragments
                    .iter()
                    .position(|f| f.start >= to)
                    .unwrap_or(ring.fragments.len());
                if start >= end {
                    return Ok(Value::Null);
                }
                (
                    ring.fragments[start].start,
                    (
                        ring.init.clone(),
                        ring.fragments.range(start..end).cloned().collect(),
                    ),
                )
            };
            snapshot(Path::new(path), media)?;
            Ok(json!(first))
        }
        _ => bail!("Unknown capture operation"),
    }
}

fn run() -> Result<()> {
    let mut args = std::env::args().skip(1);
    let first = args.next().context("Missing replay duration")?;
    #[cfg(windows)]
    if first == "dxgi-outputs" {
        return dxgi_outputs();
    }
    let seconds: f64 = first.parse()?;
    let limit: usize = args.next().context("Missing replay budget")?.parse()?;
    ensure!(
        seconds.is_finite() && seconds > 0.0 && limit > 0,
        "Invalid replay limits"
    );
    let state = Arc::new(Mutex::new(Buffer {
        epoch: Instant::now(),
        paused_at: None,
        seconds,
        limit,
        bytes: 0,
        rings: BTreeMap::new(),
    }));
    let mut children = BTreeMap::new();
    let mut output = io::stdout().lock();
    for line in io::stdin().lock().lines() {
        let line = line?;
        let result = serde_json::from_str::<Value>(&line)
            .map_err(anyhow::Error::from)
            .and_then(|request| handle(request, &state, &mut children));
        let reply = match result {
            Ok(data) => json!({"ok": true, "data": data}),
            Err(error) => json!({"ok": false, "error": format!("{error:#}")}),
        };
        if writeln!(output, "{reply}")
            .and_then(|_| output.flush())
            .is_err()
        {
            break;
        }
    }
    for (_, child) in children {
        child.stop();
    }
    Ok(())
}

#[cfg(windows)]
fn dxgi_outputs() -> Result<()> {
    use windows::Win32::Graphics::Dxgi::Common::DXGI_MODE_ROTATION_IDENTITY;
    use windows::Win32::Graphics::Dxgi::{CreateDXGIFactory1, DXGI_ERROR_NOT_FOUND, IDXGIFactory1};

    let factory: IDXGIFactory1 = unsafe { CreateDXGIFactory1()? };
    let mut outputs = Vec::new();
    for adapter_index in 0.. {
        // Match FFmpeg's d3d11va adapter numbering.
        let adapter = match unsafe { factory.EnumAdapters(adapter_index) } {
            Ok(adapter) => adapter,
            Err(error) if error.code() == DXGI_ERROR_NOT_FOUND => break,
            Err(error) => return Err(error.into()),
        };
        for output_index in 0.. {
            let output = match unsafe { adapter.EnumOutputs(output_index) } {
                Ok(output) => output,
                Err(error) if error.code() == DXGI_ERROR_NOT_FOUND => break,
                Err(error) => return Err(error.into()),
            };
            let desc = unsafe { output.GetDesc()? };
            if desc.AttachedToDesktop.as_bool() {
                let name = String::from_utf16_lossy(&desc.DeviceName);
                outputs.push(serde_json::json!({
                    "name": name.trim_end_matches('\0'),
                    "adapter": adapter_index,
                    "output": output_index,
                    "rotated": desc.Rotation != DXGI_MODE_ROTATION_IDENTITY,
                }));
            }
        }
    }
    println!("{}", serde_json::to_string(&outputs)?);
    Ok(())
}

fn main() -> Result<()> {
    run()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn atom(kind: &[u8; 4], payload: &[u8]) -> Vec<u8> {
        let mut data = ((payload.len() + 8) as u32).to_be_bytes().to_vec();
        data.extend_from_slice(kind);
        data.extend_from_slice(payload);
        data
    }

    fn fragment(time: u64) -> Fragment {
        let moof = atom(
            b"moof",
            &atom(
                b"traf",
                &atom(b"tfdt", &[&[1, 0, 0, 0][..], &time.to_be_bytes()].concat()),
            ),
        );
        let (timestamp_offset, timestamp, wide) = timestamp(&moof).unwrap();
        Fragment {
            moof,
            mdat: atom(b"mdat", &[42]),
            timestamp,
            timestamp_offset,
            wide,
            start: time as f64,
            end: time as f64 + 1.0,
        }
    }

    #[test]
    fn snapshot_rebases_timestamp_without_copying_media_payload() {
        let fragment = fragment(90_000);
        let mut output = Vec::new();
        fragment.write(&mut output, 89_990).unwrap();
        let mut input = output.as_slice();
        assert_eq!(
            timestamp(&read_box(&mut input).unwrap().unwrap())
                .unwrap()
                .1,
            10
        );
        assert_eq!(read_box(&mut input).unwrap().unwrap(), fragment.mdat);
    }

    #[test]
    fn duration_counts_default_and_explicit_sample_lengths() {
        let mut header = [0u8; 12];
        header[3] = 8;
        header[8..].copy_from_slice(&3000u32.to_be_bytes());
        let default_run = [0u32, 2].map(u32::to_be_bytes).concat();
        let explicit_run = [0x105u32, 2, 0, 0, 1000, 2000]
            .map(u32::to_be_bytes)
            .concat();
        let track = [
            atom(b"tfhd", &header),
            atom(b"trun", &default_run),
            atom(b"trun", &explicit_run),
        ]
        .concat();
        assert_eq!(
            fragment_duration(&atom(b"moof", &atom(b"traf", &track))),
            Some(9000)
        );
    }

    #[test]
    fn shared_budget_evicts_oldest_across_tracks() {
        let old = Arc::new(fragment(0));
        let new = Arc::new(fragment(1));
        let mut buffer = Buffer {
            epoch: Instant::now(),
            paused_at: None,
            seconds: 10.0,
            limit: old.bytes(),
            bytes: old.bytes() + new.bytes(),
            rings: BTreeMap::from([
                (
                    1,
                    Ring {
                        started: Some(0.0),
                        fragments: VecDeque::from([old]),
                        ..Ring::default()
                    },
                ),
                (
                    2,
                    Ring {
                        started: Some(0.0),
                        fragments: VecDeque::from([new]),
                        ..Ring::default()
                    },
                ),
            ]),
        };
        buffer.trim();
        assert!(buffer.rings[&1].fragments.is_empty());
        assert_eq!(buffer.rings[&2].fragments.len(), 1);
        assert_eq!(buffer.bytes, buffer.limit);
    }

    #[test]
    fn truncated_mp4_payload_is_rejected() {
        let data = atom(b"mdat", &[0, 1]);
        assert!(read_box(&mut &data[..data.len() - 1]).is_err());
    }
}
