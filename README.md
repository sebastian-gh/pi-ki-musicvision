# Pi-KI-MusicVision

**Real-time music generation from human movement — dance becomes sound.**

> ⚠️ **Early-stage / Work in Progress:** This repository documents the first experimental steps of an ongoing art and technology project. The current implementation is a proof of concept focused on MIDI output. The long-term goal is full real-time audio synthesis driven by body movement. Expect rapid changes, incomplete features, and evolving architecture.

Pi-KI-MusicVision uses a Raspberry Pi 5 with the [Pi AI HAT+ 2 (Hailo-10H)](https://www.raspberrypi.com/products/ai-hat/) and one or two cameras to perform real-time human pose estimation. The detected body keypoints (17 landmarks per person) are translated live into musical events, turning a dancer's movements into sound.

> *"The dancer becomes the instrument. Every gesture, every leap, a note."*

---

## Project Phases

This project is developed in stages. The current repository reflects **Phase 1** only.

| Phase | Focus | Status |
|---|---|---|
| **Phase 1** | Pose estimation → MIDI output (proof of concept) | In progress |
| **Phase 2** | Stereo cameras, depth (Z-axis) mapping, multi-person | Planned |
| **Phase 3** | Real-time audio synthesis (beyond MIDI) | Planned |
| **Phase 4** | Performance-ready system, live art installation | Planned |

The ultimate vision is a system that generates and synthesizes sound directly from body movement in real time, MIDI is a stepping stone, not the destination.

---

## Current State (Phase 1)

A camera watches the performer. An AI running on the Hailo-10H NPU detects the full body skeleton in real time (~30 fps). As a first experiment, hand and shoulder positions are mapped to MIDI events:

- **Pitch hand** (left hand in mirror view): vertical position selects the musical note
- **Trigger hand** (right hand in mirror view): raised above the shoulder triggers Note On; lowered triggers Note Off
- **MOD Wheel**: height of the trigger hand above the shoulder controls MIDI CC#1 (modulation)
- **Scales**: chromatic, major, minor, pentatonic, blues (configurable)

The MIDI output connects to any synthesizer, DAW, or software instrument. This two-hand mapping is an **early experiment** to explore how expressive body control can feel, and to build the technical foundation for more complex sound generation.

---

## Current MIDI Mapping (`pi-ki-musicvision.py`)

```
Left hand (mirrored view)   ->  Pitch
  Y position (up/down)        ->  Note (high = high note, low = low note)

Right hand (mirrored view)  ->  Trigger + Modulation
  Above shoulder              ->  Note ON
  Below shoulder              ->  Note OFF
  Height above shoulder       ->  MOD Wheel (CC#1, 0–127)
```

| Control | Body Part | Mapping |
|---|---|---|
| Note pitch | Left wrist (Y) | Top of frame = highest note, bottom = lowest |
| Note on/off | Right wrist vs. right shoulder | Above shoulder = ON, below = OFF |
| Modulation (CC#1) | Right wrist height | Distance above shoulder → 0–127 |

Hysteresis prevents accidental trigger flicker and rapid note trilling at boundaries.

---

## Hardware Requirements

| Component | Details |
|---|---|
| **Raspberry Pi 5** | 4 GB or 8 GB RAM recommended |
| **Pi AI HAT+ 2** | Hailo-10H, required for real-time inference |
| **Camera** | Raspberry Pi Camera Module 3 or USB webcam |
| **Optional** | Second camera for stereo / depth estimation (Phase 2) |
| **MIDI interface** | USB-MIDI adapter or software synthesizer (virtual MIDI port supported) |

---

## Software Dependencies

This project is built on top of the [Hailo Application Suite](https://github.com/hailo-ai/hailo-apps-infra) for Raspberry Pi.

```
hailo-apps-infra     # Hailo pipeline framework (GStreamer + Python)
python-rtmidi        # MIDI output
opencv-python        # Frame processing and visualization
gstreamer-1.0        # Video pipeline
```

### Installation

1. Follow the [Hailo Apps setup guide](https://github.com/hailo-ai/hailo-apps-infra) to install the base framework on your Raspberry Pi 5.

2. Install additional dependencies:
```bash
pip install python-rtmidi
```

3. Clone this repository into the `pipeline_apps` directory of your hailo-apps installation, so it sits alongside the original app folders:
```bash
cd hailo_apps/python/pipeline_apps
git clone https://github.com/sebastian-gh/pi-ki-musicvision.git
```

---

## Usage

From inside the `pi-ki-musicvision` directory:

```bash
python pi-ki-musicvision.py --input rpi --use-frame
```

Use `--input usb` for a USB webcam, or `--input /dev/videoX` for a specific device.

Press `Ctrl+C` to stop. All notes are silenced cleanly on exit.

---

## Configuration

Edit the `MIDI_CONFIG` dictionary at the top of `pi-ki-musicvision.py`:

```python
MIDI_CONFIG = {
    "port_index":         1,             # MIDI port index (None = virtual port "DanceMIDI")
    "channel":            0,             # MIDI channel (0 = channel 1)
    "note_min":           48,            # Lowest note (C3)
    "note_max":           72,            # Highest note (C5)
    "scale":              "pentatonic",  # chromatic | major | minor | pentatonic | blues
    "root":               0,             # Root note (0=C, 2=D, 4=E, 5=F, 7=G, 9=A, 11=B)
    "velocity":           100,           # Fixed note velocity
    "min_confidence":     0.5,           # Minimum keypoint confidence threshold
    "note_cooldown":      0.08,          # Seconds between note changes (anti-jitter)
    "trigger_hysteresis": 0.03,          # Normalized Y offset for trigger dead-zone
    "note_hysteresis":    0.02,          # Normalized Y change required for note switch
    "mod_wheel_enabled":  True,          # Enable/disable MOD wheel CC
}
```

---

## Project Structure

This repository lives as its own folder inside the hailo-apps `pipeline_apps` directory, separate from the original Hailo example code:

```
pipeline_apps/
├── pose_estimation/             # Original Hailo example (unmodified, not part of this repo)
│   ├── pose_estimation.py
│   ├── pose_estimation_pipeline.py
│   └── __init__.py
│
└── pi-ki-musicvision/           # This repository (pi-ki-musicvision)
    ├── pi-ki-musicvision.py     # Phase 1: two-hand MIDI experiment
    ├── pose_estimation_pipeline.py
    ├── __init__.py
    ├── README.md
    └── LICENSE
```

---

## Artistic Vision

Pi-KI-MusicVision is developed as an **interactive art project** at the intersection of contemporary dance, artificial intelligence, and live music generation. The system is designed for live performance and gallery installation contexts, where dancers perform without knowing exactly what music will emerge, the AI mediates between body and sound in real time.

The long-term goal goes beyond MIDI control: the system should generate and synthesize its own sound directly, making the connection between body and music as immediate and expressive as possible.

The project is based in **Berlin** and is currently in active development toward a first public performance in 2027.

---

## License

This project is licensed under the **MIT License**, see [LICENSE](LICENSE) for details.

The underlying Hailo pipeline framework ([hailo-apps-infra](https://github.com/hailo-ai/hailo-apps-infra)) is subject to its own license terms.

---

## Acknowledgements

- [Hailo AI](https://hailo.ai/) for the Pi AI HAT+ 2 and the hailo-apps-infra framework
- [python-rtmidi](https://spotlightkid.github.io/python-rtmidi/) for robust Python MIDI I/O
- The open-source GStreamer and OpenCV communities

---

*Built with a Raspberry Pi 5, a camera, and the belief that a body in motion is already music.*
