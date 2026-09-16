//! A sample ring buffer in shared memory, so the analysis process can read the
//! IQ for an event without the reader copying it.
//!
//! In the Python engine `Ring.get` copied up to ~96 MB of IQ per analysed event
//! on the reader thread at 10 MSPS. Here the reader only ever writes; the
//! analysis process maps the same file and takes its own copy when it gets to
//! the job, on its own core.
//!
//! Layout, little-endian, header 64 bytes then `capacity` complex32 samples
//! (float32 real, float32 imag — numpy's complex64 memory layout):
//!
//! ```text
//!   0  u64  magic "RFSVRING"
//!   8  u64  capacity, samples
//!  16  u64  written — absolute count of samples ever written this generation
//!  24  u64  generation — bumped on every reset (retune)
//!  32  f64  sample rate
//!  40  f64  centre frequency
//!  48  u64  reserved
//!  56  u64  reserved
//! ```
//!
//! Single writer. `written` is advanced only after the samples it covers are in
//! place, and a reader must re-check `written` and `generation` after copying: if
//! either moved far enough to overwrite what it copied, the copy is discarded.
//! A 64-bit store is one instruction on aarch64, so a reader never sees a torn
//! counter.

use memmap2::MmapMut;
use num_complex::Complex32;
use std::fs::OpenOptions;
use std::path::Path;

const MAGIC: u64 = u64::from_le_bytes(*b"RFSVRING");
pub const HEADER: usize = 64;

pub struct ShmRing {
    map: MmapMut,
    capacity: u64,
    written: u64,
    generation: u64,
}

impl ShmRing {
    pub fn create(path: &Path, capacity: u64, rate: f64) -> std::io::Result<Self> {
        let bytes = HEADER as u64 + capacity * 8;
        let file = OpenOptions::new().read(true).write(true).create(true).truncate(true).open(path)?;
        file.set_len(bytes)?;
        // SAFETY: the file is ours, sized above, and only this process writes it.
        let map = unsafe { MmapMut::map_mut(&file)? };
        let mut ring = ShmRing { map, capacity, written: 0, generation: 0 };
        ring.put_u64(0, MAGIC);
        ring.put_u64(8, capacity);
        ring.put_u64(16, 0);
        ring.put_u64(24, 0);
        ring.put_f64(32, rate);
        ring.put_f64(40, 0.0);
        Ok(ring)
    }

    fn put_u64(&mut self, off: usize, v: u64) {
        self.map[off..off + 8].copy_from_slice(&v.to_le_bytes());
    }

    fn put_f64(&mut self, off: usize, v: f64) {
        self.map[off..off + 8].copy_from_slice(&v.to_le_bytes());
    }

    pub fn capacity(&self) -> u64 {
        self.capacity
    }

    pub fn written(&self) -> u64 {
        self.written
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    /// Forget everything: a retune makes old IQ a different part of the spectrum.
    pub fn reset(&mut self, center_hz: f64) {
        self.generation += 1;
        self.written = 0;
        // Generation first, so a reader mid-copy sees it change before the
        // counter goes back to zero.
        self.put_u64(24, self.generation);
        self.put_u64(16, 0);
        self.put_f64(40, center_hz);
    }

    pub fn push(&mut self, samples: &[Complex32]) {
        let cap = self.capacity as usize;
        let mut x = samples;
        if x.len() >= cap {
            // Only the last `cap` survive, but they must land where their
            // absolute indices map to (see Ring.push in Python).
            let drop = x.len() - cap;
            self.written += drop as u64;
            x = &x[drop..];
        }
        let n = x.len();
        let pos = (self.written % self.capacity) as usize;
        let split = (cap - pos).min(n);
        self.copy_in(pos, &x[..split]);
        if split < n {
            self.copy_in(0, &x[split..]);
        }
        self.written += n as u64;
        self.put_u64(16, self.written);
    }

    fn copy_in(&mut self, slot: usize, src: &[Complex32]) {
        let start = HEADER + slot * 8;
        let dst = &mut self.map[start..start + src.len() * 8];
        // SAFETY: Complex32 is repr(C) { re: f32, im: f32 } — 8 bytes, no padding.
        let bytes = unsafe { std::slice::from_raw_parts(src.as_ptr() as *const u8, src.len() * 8) };
        dst.copy_from_slice(bytes);
    }

    /// Whether `[start, start+len)` is still held — `Ring.get` returning non-None.
    pub fn holds(&self, start: u64, len: u64) -> bool {
        len > 0 && start + len <= self.written && start + self.capacity >= self.written
    }
}
