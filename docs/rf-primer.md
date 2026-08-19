# RF concepts, explained for someone who knows ADCs and Linux

Read this once and the other documents stop being cryptic. Nothing here is hard — it's
mostly stuff you already understand with unfamiliar names attached.

---

## 1. An SDR is a fast ADC with a tuner bolted on the front

That's genuinely all it is.

1. The **tuner** takes a chunk of radio spectrum and shifts it down near zero, the same way
   a mixer stage works in any superhet receiver.
2. The **ADC** samples that chunk.
3. You get a stream of samples over USB.

Everything you know about ADCs applies directly:

| ADC concept | SDR equivalent |
|---|---|
| Sample rate | How wide a chunk you see at once. 6 MSPS = 6 MHz of spectrum. |
| Bit depth | Dynamic range. RTL-SDR is 8-bit, Airspy is 12-bit. |
| Clipping | Same thing, and it's the main failure mode in this project. |
| Input voltage range | "Compression point" — the level where it stops behaving linearly. |

The one genuinely new thing: samples are **complex numbers**, not scalars. Each sample is a
pair (I, Q). You don't need the math — just know that a "sample" is two floats, and that's
why data rates are higher than you'd expect. 6 MSPS of complex float32 is ~48 MB/s if
unpacked.

---

## 2. dB and dBm are just a log scale

Radio signals span about 14 orders of magnitude, so nobody writes them in watts.

| Change | Means |
|---|---|
| +3 dB | 2× the power |
| +10 dB | 10× |
| +20 dB | 100× |
| +30 dB | 1000× |
| −20 dB | 1/100 |

**dBm** is dB relative to 1 milliwatt:

| dBm | Actual power | What it is |
|---|---|---|
| +37 dBm | 5 W | a handheld radio transmitting |
| 0 dBm | 1 mW | very strong received signal |
| −60 dBm | 1 nW | strong received signal |
| −100 dBm | 0.1 pW | typical weak signal |
| −130 dBm | — | thermal noise floor, the physical limit |

The only arithmetic you need: **dB values add.** Signal at −40 dBm through a 20 dB
attenuator comes out at −60 dBm. That's it.

---

## 3. The core problem: it's an ADC clipping problem

If you feed too much voltage into your MCU's ADC, you clip and get garbage. Same here, with
one nasty extra wrinkle.

When an RF front end clips, it doesn't just distort *that* signal. The non-linearity
multiplies signals together and produces **new signals at frequencies where nothing is
actually transmitting.** In RF this is called *intermodulation*, or "intermod."

For this project that's the worst possible failure, because the deck's whole job is finding
channels. Intermod makes it find channels that don't exist. It doesn't crash, doesn't warn
you — you just get a log full of fiction.

There's a second symptom called **desense**: one very strong signal drives the front end
into compression and everything else gets quieter. Your receiver goes deaf while the strong
signal is present.

Both have the same cure: **put less signal in.**

---

## 4. An attenuator is a voltage divider

Literally. It's a resistor network in a little metal can with connectors on both ends. A
"20 dB pad" divides the power by 100.

Counter-intuitive but true for this project: **you want one permanently installed.**

At a festival, everything you care about is within a couple of kilometres, so every signal
arrives *loud* — around 65 dB above the noise floor. You have far more sensitivity than you
need and nowhere near enough headroom. Trading 20 dB of sensitivity you'll never use for 20
dB of clipping protection is free money.

A **filter** is the other half of the same strategy — a passive LC network that blocks
frequencies you don't want. The "FM notch" blocks 88–108 MHz so nearby FM broadcast stations
(which are enormously more powerful than handheld radios) never reach the ADC.

---

## 5. FFT does the heavy lifting, and it's cheaper than you'd guess

You have a 6 MHz chunk of spectrum arriving as samples. You want to know which of the ~960
narrow channels inside it are active.

Naive approach: filter out each channel separately, 960 times. Expensive.

Actual approach: **one FFT.** An FFT of the whole chunk gives you power at every frequency
simultaneously — so you get all 960 channels for roughly the cost of one operation.

The closest analogy from your world: reading a whole GPIO port in one register read instead
of polling 32 pins individually.

This is why the design says "monitor everything continuously" rather than "scan." A scanner
steps through channels one at a time and misses whatever happens while it's looking
elsewhere. We just look at all of them, always.

---

## 6. FM, and what "deviation" means

- **AM**: the signal's *amplitude* carries the information.
- **FM**: the signal's *frequency* wiggles back and forth. That wiggle carries the audio.

**Deviation** is how far it wiggles. Voice radios use roughly ±2.5 kHz (narrowband) or
±5 kHz (wideband).

To decode FM: measure how fast the phase is rotating from sample to sample. That rotation
rate *is* the audio. It's about three lines of code — `np.angle(x[1:] * conj(x[:-1]))`.

That single measurement also drives the classifier:

| What you see | What it is |
|---|---|
| Smooth, continuous wiggle | analog voice |
| Wiggle snaps between 4 fixed levels | digital (DMR, P25) |
| Wiggle snaps between 2 levels | simpler digital (pager, data) |
| No wiggle | unmodulated carrier |

---

## 7. CTCSS and DCS are a squelch password, not encryption

Lots of radios share the same frequency. Nobody wants to hear the other groups.

So each group's radios transmit a **constant quiet tone underneath the voice** — between 67
and 254 Hz, below what you really hear. Your radio stays muted unless it hears *your* tone.
There are about 50 standard tones.

That's **CTCSS**. Motorola calls it "PL." FRS radios call them "privacy codes," which is
marketing nonsense — it provides zero privacy. Anyone listening without tone squelch hears
everything.

**DCS** does the same job with a repeating digital code instead of a steady tone. Same
purpose, different mechanism.

**Why this project cares:** to talk on a channel, you need to know its tone. A frequency
alone isn't enough. Getting the tone is most of the difference between "I found a channel"
and "I can use this channel."

---

## 8. Repeaters listen on one frequency and transmit on another

A repeater sits somewhere high, receives on frequency A, and simultaneously rebroadcasts on
frequency B. That's how two handhelds miles apart talk to each other.

| Band | Gap between the two ("offset") |
|---|---|
| 2 m (144–148 MHz) | 600 kHz |
| 70 cm (440–450 MHz) | 5 MHz |
| GMRS (462/467 MHz) | 5 MHz |

**The critical bit, and the reason this whole project is shaped the way it is:**

- When you *listen*, you hear the **output**.
- When you *talk*, you transmit on the **input**.
- The input usually requires a tone to open the repeater up.
- **That tone is often not the tone the repeater sends back out.**

So monitoring the output tells you the frequency but not how to get in. The only way to
learn the access tone is to catch a *user* transmitting on the input frequency.

That's why the design picks receiver windows that cover both halves at once. 462–468 MHz
contains every GMRS output *and* every GMRS input, so one receiver sees both sides of every
conversation.

---

## 9. A codeplug is a radio's config file

The set of channels programmed into a radio: frequency, offset, tone, mode, name. CHIRP is
the common open-source tool for editing them.

We're not producing codeplugs as output — the database is the product. But "could I fill in
a complete codeplug entry from this row?" is the test for whether a discovered channel is
actually *useful*, which is where the tiers come from:

| Tier | You know | Can you talk? |
|---|---|---|
| **T0** | A frequency was busy | No |
| **T1** | + what kind of signal it is | No |
| **T2** | + the tone → **full simplex entry** | Yes, simplex |
| **T3** | + the input frequency and *its* tone | Yes, through the repeater |
| **T4** | + digital details (color code, talkgroup) | Yes, digital |

**T2 is the bar.** Anything below that is trivia.

---

## 10. What the deck actually does

Stripped of RF vocabulary, it's an event logger — a shape you already know:

```
antenna → attenuator → filter → SDR → USB → Pi
                                              │
                    FFT, ~70 times per second │
                                              ▼
                          power level for every channel
                                              │
                    "did any channel just get louder?"
                                              ▼
                                    start/stop events
                                              │
                    for active channels: decode audio,
                    measure the tone, work out the mode
                                              ▼
                                      SQLite rows
```

The parts that are genuinely fiddly:

1. **Not clipping** (§3, §4) — solved with a permanent attenuator and a filter.
2. **Telling a real tone from a DCS code** — these look similar and confusing them silently
   corrupts your whole log.
3. **Matching repeater input to output** — the novel part, and the one with no existing
   software to copy from.

Everything else is plumbing you'd be comfortable with: USB device handling, a service that
restarts on failure, disk rotation, SQLite, systemd.

---

## 11. Terms you'll hit in the other docs

| Term | Plain version |
|---|---|
| **IQ / complex samples** | Each sample is two numbers instead of one |
| **dBFS** | How close to ADC clipping, 0 = clipping |
| **Noise floor** | The background hiss level; anything below it is invisible |
| **SNR** | How far above the noise a signal is, in dB |
| **Front end** | Everything before the ADC: antenna, filters, amplifiers |
| **Intermod** | Fake signals created by clipping |
| **Desense** | Receiver goes deaf because something strong is nearby |
| **Compression point** | The input level where it stops being linear (≈ clipping) |
| **Dynamic range** | Gap between the weakest and strongest it can handle at once |
| **Pad / attenuator** | Resistive divider that reduces signal |
| **Notch filter** | Blocks one band, passes everything else |
| **Simplex** | Radio-to-radio direct, one frequency |
| **Duplex** | Through a repeater, two frequencies |
| **Offset** | The gap between a repeater's two frequencies |
| **CTCSS / PL / "privacy code"** | Sub-audible tone used as a squelch password |
| **DCS** | Same idea, digital code instead of a tone |
| **Deviation** | How far FM wiggles the frequency |
| **Squelch** | Auto-mute when there's no signal |
| **Channelizer** | Splitting one wide capture into many narrow channels |
| **PPM error** | Crystal frequency error, like clock drift on an MCU |
| **ERP** | Effective transmit power including antenna gain |
| **Codeplug** | A radio's config file |
| **FRS / GMRS / MURS** | License-free and light-license consumer radio services |
| **2 m / 70 cm** | Ham slang for the 144–148 and 420–450 MHz bands |
