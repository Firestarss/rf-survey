//! The minimal slice of the SoapySDR 0.8 C API the engine uses.
//!
//! Hand-written rather than generated: fifteen functions, and it keeps bindgen
//! and libclang out of the build on the deck. Signatures are copied from
//! /usr/include/SoapySDR/Device.h as shipped by libsoapysdr-dev 0.8.1 — note
//! that setupStream *returns* the stream in 0.8; in 0.7 it was an out-parameter,
//! so these declarations are not valid against 0.7.
//!
//! The radio is driven through SoapyAirspy exactly as the Python engine drives
//! it, so the overall 0-45 gain that every Phase 1 measurement was taken with
//! means the same thing here. Talking to libairspy directly would have meant
//! re-deriving how SoapyAirspy splits that one number across LNA, mixer and VGA.

use libc::{c_char, c_double, c_int, c_long, c_longlong, c_void, size_t};
use std::ffi::{CStr, CString};

pub const SOAPY_SDR_RX: c_int = 1;
pub const SOAPY_SDR_TIMEOUT: c_int = -1;
pub const SOAPY_SDR_OVERFLOW: c_int = -4;

#[repr(C)]
struct Kwargs {
    size: size_t,
    keys: *mut *mut c_char,
    vals: *mut *mut c_char,
}

#[repr(C)]
struct DeviceT {
    _p: [u8; 0],
}

#[repr(C)]
struct StreamT {
    _p: [u8; 0],
}

#[link(name = "SoapySDR")]
extern "C" {
    fn SoapySDRDevice_lastError() -> *const c_char;
    fn SoapySDRDevice_makeStrArgs(args: *const c_char) -> *mut DeviceT;
    fn SoapySDRDevice_unmake(device: *mut DeviceT) -> c_int;
    fn SoapySDRDevice_getHardwareInfo(device: *const DeviceT) -> Kwargs;
    fn SoapySDRKwargs_get(args: *const Kwargs, key: *const c_char) -> *const c_char;
    fn SoapySDRKwargs_clear(args: *mut Kwargs);
    fn SoapySDRDevice_setSampleRate(device: *mut DeviceT, dir: c_int, ch: size_t, rate: c_double) -> c_int;
    fn SoapySDRDevice_getSampleRate(device: *const DeviceT, dir: c_int, ch: size_t) -> c_double;
    fn SoapySDRDevice_setFrequency(device: *mut DeviceT, dir: c_int, ch: size_t, freq: c_double, args: *const Kwargs) -> c_int;
    fn SoapySDRDevice_getFrequency(device: *const DeviceT, dir: c_int, ch: size_t) -> c_double;
    fn SoapySDRDevice_setGainMode(device: *mut DeviceT, dir: c_int, ch: size_t, automatic: bool) -> c_int;
    fn SoapySDRDevice_setGain(device: *mut DeviceT, dir: c_int, ch: size_t, value: c_double) -> c_int;
    fn SoapySDRDevice_setupStream(
        device: *mut DeviceT,
        dir: c_int,
        format: *const c_char,
        channels: *const size_t,
        num_chans: size_t,
        args: *const Kwargs,
    ) -> *mut StreamT;
    fn SoapySDRDevice_getStreamMTU(device: *const DeviceT, stream: *mut StreamT) -> size_t;
    fn SoapySDRDevice_activateStream(device: *mut DeviceT, stream: *mut StreamT, flags: c_int, time_ns: c_longlong, num_elems: size_t) -> c_int;
    fn SoapySDRDevice_deactivateStream(device: *mut DeviceT, stream: *mut StreamT, flags: c_int, time_ns: c_longlong) -> c_int;
    fn SoapySDRDevice_closeStream(device: *mut DeviceT, stream: *mut StreamT) -> c_int;
    fn SoapySDRDevice_readStream(
        device: *mut DeviceT,
        stream: *mut StreamT,
        buffs: *const *mut c_void,
        num_elems: size_t,
        flags: *mut c_int,
        time_ns: *mut c_longlong,
        timeout_us: c_long,
    ) -> c_int;
}

fn last_error() -> String {
    // SAFETY: returns a static NUL-terminated string owned by SoapySDR.
    unsafe {
        let p = SoapySDRDevice_lastError();
        if p.is_null() {
            String::from("(no error text)")
        } else {
            CStr::from_ptr(p).to_string_lossy().into_owned()
        }
    }
}

pub struct Device {
    dev: *mut DeviceT,
    stream: *mut StreamT,
}

// The device is only ever used from the reader thread after construction.
unsafe impl Send for Device {}

impl Device {
    /// `args` is exactly the string Python's device_args() builds.
    pub fn open(args: &str) -> Result<Self, String> {
        let c = CString::new(args).map_err(|e| e.to_string())?;
        // SAFETY: valid C string; returns NULL on failure.
        let dev = unsafe { SoapySDRDevice_makeStrArgs(c.as_ptr()) };
        if dev.is_null() {
            return Err(format!("could not open device '{args}': {}", last_error()));
        }
        Ok(Device { dev, stream: std::ptr::null_mut() })
    }

    /// The serial read back off the hardware, not the one that was asked for.
    pub fn serial(&self) -> Option<String> {
        // SAFETY: kwargs returned by value is cleared below; key is NUL-terminated.
        unsafe {
            let mut info = SoapySDRDevice_getHardwareInfo(self.dev);
            let key = CString::new("serial").unwrap();
            let v = SoapySDRKwargs_get(&info, key.as_ptr());
            let out = if v.is_null() { None } else { Some(CStr::from_ptr(v).to_string_lossy().into_owned()) };
            SoapySDRKwargs_clear(&mut info);
            out
        }
    }

    pub fn set_sample_rate(&mut self, rate: f64) -> Result<f64, String> {
        // SAFETY: plain calls on a valid device.
        unsafe {
            if SoapySDRDevice_setSampleRate(self.dev, SOAPY_SDR_RX, 0, rate) != 0 {
                return Err(format!("setSampleRate: {}", last_error()));
            }
            Ok(SoapySDRDevice_getSampleRate(self.dev, SOAPY_SDR_RX, 0))
        }
    }

    pub fn set_frequency(&mut self, hz: f64) -> Result<f64, String> {
        // SAFETY: NULL kwargs is accepted by the C API.
        unsafe {
            if SoapySDRDevice_setFrequency(self.dev, SOAPY_SDR_RX, 0, hz, std::ptr::null()) != 0 {
                return Err(format!("setFrequency: {}", last_error()));
            }
            Ok(SoapySDRDevice_getFrequency(self.dev, SOAPY_SDR_RX, 0))
        }
    }

    pub fn set_gain_manual(&mut self) -> bool {
        // SAFETY: plain call on a valid device.
        unsafe { SoapySDRDevice_setGainMode(self.dev, SOAPY_SDR_RX, 0, false) == 0 }
    }

    pub fn set_gain(&mut self, db: f64) -> Result<(), String> {
        // SAFETY: plain call on a valid device.
        unsafe {
            if SoapySDRDevice_setGain(self.dev, SOAPY_SDR_RX, 0, db) != 0 {
                return Err(format!("setGain: {}", last_error()));
            }
        }
        Ok(())
    }

    /// Sets up and activates a CF32 stream; returns the stream MTU.
    pub fn start(&mut self) -> Result<usize, String> {
        let fmt = CString::new("CF32").unwrap();
        let chans: [size_t; 1] = [0];
        // SAFETY: format and channel list outlive the call; NULL args accepted.
        unsafe {
            let s = SoapySDRDevice_setupStream(self.dev, SOAPY_SDR_RX, fmt.as_ptr(), chans.as_ptr(), 1, std::ptr::null());
            if s.is_null() {
                return Err(format!("setupStream: {}", last_error()));
            }
            self.stream = s;
            if SoapySDRDevice_activateStream(self.dev, s, 0, 0, 0) != 0 {
                return Err(format!("activateStream: {}", last_error()));
            }
            Ok(SoapySDRDevice_getStreamMTU(self.dev, s))
        }
    }

    /// Samples into `buf` (interleaved f32 pairs). Returns the count, or a
    /// negative SoapySDR status: SOAPY_SDR_OVERFLOW, SOAPY_SDR_TIMEOUT, ...
    pub fn read(&mut self, buf: &mut [num_complex::Complex32], timeout_us: i64) -> i32 {
        let mut flags: c_int = 0;
        let mut t: c_longlong = 0;
        let ptrs: [*mut c_void; 1] = [buf.as_mut_ptr() as *mut c_void];
        // SAFETY: buf is valid for buf.len() CF32 elements for the call.
        unsafe { SoapySDRDevice_readStream(self.dev, self.stream, ptrs.as_ptr(), buf.len(), &mut flags, &mut t, timeout_us as c_long) }
    }
}

impl Drop for Device {
    fn drop(&mut self) {
        // SAFETY: stream and device were created by this struct.
        unsafe {
            if !self.stream.is_null() {
                SoapySDRDevice_deactivateStream(self.dev, self.stream, 0, 0);
                SoapySDRDevice_closeStream(self.dev, self.stream);
            }
            SoapySDRDevice_unmake(self.dev);
        }
    }
}
