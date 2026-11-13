// SPDX-License-Identifier: GPL-3.0-only
use anyhow::{Result, bail};
use std::path::{Path, PathBuf};

pub struct Capture {
    pub file: PathBuf,
    pub duration: f64,
}
pub struct Voiceover {
    pub ready: Option<Capture>,
    pub error: Option<String>,
    pub stopping: bool,
}
pub fn devices(_program: &Path) -> Result<Vec<(String, String)>> {
    bail!("Voiceover capture is currently unavailable on Windows.")
}
impl Voiceover {
    pub fn start(_program: PathBuf, _device: String, _cache: PathBuf) -> Self {
        Self {
            ready: None,
            error: Some("Voiceover capture is currently unavailable on Windows.".into()),
            stopping: false,
        }
    }
    pub fn elapsed(&self) -> f64 {
        0.0
    }
    pub fn finish(&mut self) {
        self.stopping = true;
    }
    pub fn poll(&mut self) {}
}
