// SPDX-License-Identifier: GPL-3.0-only
use std::io::{self, Read};
use std::process::{Child, Command};

pub fn python() -> Command {
    let program = std::env::var_os("LAYER_PYTHON")
        .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.into());
    let mut command = Command::new(program);
    command.env("PYTHONUTF8", "1");
    command
}

pub trait MediaCommand {
    fn isolate(&mut self) -> &mut Self;
}

impl MediaCommand for Command {
    fn isolate(&mut self) -> &mut Self {
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            self.process_group(0);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            self.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        self
    }
}

pub fn kill(child: &mut Child) {
    #[cfg(unix)]
    kill_group(child.id(), libc::SIGKILL);
    let _ = child.kill();
}

#[cfg(unix)]
pub fn kill_group(pid: u32, signal: i32) {
    if pid != 0 {
        unsafe {
            libc::kill(-(pid as i32), signal);
        }
    }
}

#[cfg(unix)]
pub fn nonblocking<T: std::os::fd::AsRawFd>(pipe: &T) -> bool {
    let fd = pipe.as_raw_fd();
    unsafe {
        let flags = libc::fcntl(fd, libc::F_GETFL);
        flags >= 0 && libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) >= 0
    }
}

#[cfg(unix)]
pub fn read_ready(pipe: &mut impl Read, data: &mut [u8]) -> io::Result<usize> {
    pipe.read(data)
}

#[cfg(windows)]
pub fn read_ready(pipe: &mut std::process::ChildStdout, data: &mut [u8]) -> io::Result<usize> {
    use std::{os::windows::io::AsRawHandle, ptr::null_mut};
    use windows_sys::Win32::{Foundation::ERROR_BROKEN_PIPE, System::Pipes::PeekNamedPipe};
    let mut available = 0;
    if unsafe {
        PeekNamedPipe(
            pipe.as_raw_handle(),
            null_mut(),
            0,
            null_mut(),
            &mut available,
            null_mut(),
        )
    } == 0
    {
        let error = io::Error::last_os_error();
        return if error.raw_os_error() == Some(ERROR_BROKEN_PIPE as i32) {
            Ok(0)
        } else {
            Err(error)
        };
    }
    if available == 0 {
        return Err(io::ErrorKind::WouldBlock.into());
    }
    let count = data.len().min(available as usize);
    pipe.read(&mut data[..count])
}

#[cfg(windows)]
pub struct Job(std::os::windows::io::OwnedHandle);

#[cfg(windows)]
impl Job {
    pub fn attach(child: &Child) -> io::Result<Self> {
        use std::{
            mem::size_of,
            os::windows::io::{AsRawHandle, FromRawHandle},
            ptr::null,
        };
        use windows_sys::Win32::System::JobObjects::*;
        unsafe {
            let handle = CreateJobObjectW(null(), null());
            if handle.is_null() {
                return Err(io::Error::last_os_error());
            }
            let job = Self(std::os::windows::io::OwnedHandle::from_raw_handle(handle));
            let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            ) == 0
                || AssignProcessToJobObject(handle, child.as_raw_handle()) == 0
            {
                return Err(io::Error::last_os_error());
            }
            Ok(job)
        }
    }

    pub fn terminate(&self) {
        use std::os::windows::io::AsRawHandle;
        unsafe {
            windows_sys::Win32::System::JobObjects::TerminateJobObject(self.0.as_raw_handle(), 1);
        }
    }
}
