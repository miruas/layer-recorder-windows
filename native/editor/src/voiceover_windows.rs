// SPDX-License-Identifier: GPL-3.0-only
//! Windows voiceover records one explicitly selected WASAPI microphone.
use anyhow::{Context, Result, bail};
use serde_json::Value;
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

use crate::media_process::{self as process, MediaCommand};

pub struct Capture {
    pub _directory: tempfile::TempDir,
    pub file: PathBuf,
    pub duration: f64,
}

enum Control {
    Finish,
    Cancel,
}

pub struct Voiceover {
    controls: mpsc::Sender<Control>,
    results: mpsc::Receiver<Result<Capture, String>>,
    pub ready: Option<Capture>,
    pub error: Option<String>,
    pub stopping: bool,
    started: Instant,
}

fn bounded_output(command: &mut Command) -> Result<Vec<u8>> {
    let output = tempfile::NamedTempFile::new()?;
    let mut child = command
        .isolate()
        .stdin(Stdio::null())
        .stdout(output.reopen()?)
        .stderr(Stdio::null())
        .spawn()?;
    let deadline = Instant::now() + Duration::from_secs(8);
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => return Ok(fs::read(output.path())?),
            Ok(Some(_)) => bail!("Could not read the Windows audio service response."),
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
            _ => {
                process::kill(&mut child);
                let _ = child.wait();
                bail!("The Windows audio service did not respond in time.");
            }
        }
    }
}

pub fn devices(_program: &Path) -> Result<Vec<(String, String)>> {
    let data = bounded_output(process::python().args(["-m", "katman.windows_audio", "devices"]))
        .context("List Windows microphones")?;
    let rows: Vec<Value> = serde_json::from_slice(&data)?;
    Ok(rows
        .iter()
        .filter_map(|row| {
            Some((
                row["endpoint"].as_str()?.into(),
                row["name"].as_str()?.into(),
            ))
        })
        .collect())
}

impl Voiceover {
    pub fn start(_program: PathBuf, endpoint: String, cache: PathBuf) -> Self {
        let (tx, rx) = mpsc::channel();
        let (result_tx, results) = mpsc::channel();
        thread::spawn(move || {
            let result = record(&endpoint, &cache, rx).map_err(|error| format!("{error:#}"));
            let _ = result_tx.send(result);
        });
        Self {
            controls: tx,
            results,
            ready: None,
            error: None,
            stopping: false,
            started: Instant::now(),
        }
    }

    pub fn elapsed(&self) -> f64 {
        self.started.elapsed().as_secs_f64()
    }

    pub fn finish(&mut self) {
        if !self.stopping {
            self.stopping = true;
            let _ = self.controls.send(Control::Finish);
        }
    }

    pub fn poll(&mut self) {
        for result in self.results.try_iter() {
            match result {
                Ok(capture) => self.ready = Some(capture),
                Err(error) => self.error = Some(error),
            }
        }
    }
}

impl Drop for Voiceover {
    fn drop(&mut self) {
        let _ = self.controls.send(Control::Cancel);
    }
}

fn record(endpoint: &str, cache: &Path, controls: mpsc::Receiver<Control>) -> Result<Capture> {
    fs::create_dir_all(cache)?;
    let directory = tempfile::Builder::new()
        .prefix("voiceover-")
        .tempdir_in(cache)?;
    let file = directory.path().join("Voiceover.m4a");
    let log = directory.path().join("capture.log");
    let mut child = process::python()
        .args([
            "-m",
            "katman.windows_audio",
            "capture",
            "microphone",
            endpoint,
            "0",
        ])
        .isolate()
        .stdin(Stdio::piped())
        .stdout(fs::File::create(&file)?)
        .stderr(fs::File::create(&log)?)
        .spawn()
        .context("Start microphone capture")?;
    let job = process::Job::attach(&child).inspect_err(|_| {
        process::kill(&mut child);
        let _ = child.wait();
    })?;
    let mut deadline = None;
    let result = (|| -> Result<()> {
        loop {
            if let Some(status) = child.try_wait()? {
                if deadline.is_some() && status.success() {
                    return Ok(());
                }
                let details = fs::read_to_string(&log).unwrap_or_default();
                bail!("Microphone capture stopped. {}", details.trim());
            }
            match controls.recv_timeout(Duration::from_millis(25)) {
                Ok(Control::Finish) if deadline.is_none() => {
                    deadline = Some(Instant::now() + Duration::from_secs(7));
                    if let Some(mut input) = child.stdin.take() {
                        let _ = input.write_all(b"q\n");
                    }
                }
                Ok(Control::Cancel) | Err(mpsc::RecvTimeoutError::Disconnected) => {
                    bail!("Microphone recording cancelled.");
                }
                _ => (),
            }
            if deadline.is_some_and(|limit| Instant::now() >= limit) {
                bail!("The microphone did not finish in time. No clip was added.");
            }
        }
    })();
    if result.is_err() {
        job.terminate();
        let _ = child.wait();
    }
    result?;
    let data = bounded_output(
        Command::new("ffprobe")
            .args([
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type:format=duration",
                "-of",
                "json",
            ])
            .arg(&file),
    )
    .context("Read microphone recording")?;
    let value: Value = serde_json::from_slice(&data)?;
    let duration = value["format"]["duration"]
        .as_str()
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(0.);
    if duration < 0.1
        || !value["streams"]
            .as_array()
            .is_some_and(|streams| streams.iter().any(|s| s["codec_type"] == "audio"))
    {
        bail!("The recording is too short. Record at least a moment of microphone audio.");
    }
    Ok(Capture {
        _directory: directory,
        file,
        duration,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[ignore = "requires a Windows microphone and permission to record from it"]
    fn selected_microphone_records_an_importable_voiceover() {
        let devices = devices(Path::new("")).unwrap();
        let (endpoint, _) = devices.first().expect("No microphone is available");
        let directory = tempfile::tempdir().unwrap();
        let mut voiceover =
            Voiceover::start(PathBuf::new(), endpoint.clone(), directory.path().into());
        thread::sleep(Duration::from_secs(2));
        voiceover.finish();
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            voiceover.poll();
            assert!(voiceover.error.is_none(), "{:?}", voiceover.error);
            if let Some(capture) = &voiceover.ready {
                assert!(capture.file.is_file());
                assert!(capture.duration >= 1.0);
                break;
            }
            assert!(Instant::now() < deadline, "Voiceover timed out");
            thread::sleep(Duration::from_millis(20));
        }
    }
}
