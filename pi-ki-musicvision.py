# region imports
# Standard library imports
import time

# Third-party imports
import gi

gi.require_version("Gst", "1.0")
import cv2
import rtmidi

# Local application-specific imports
import hailo
from gi.repository import Gst

from hailo_apps.python.pipeline_apps.pose_estimation.pose_estimation_pipeline import (
    GStreamerPoseEstimationApp,
)
from hailo_apps.python.core.common.buffer_utils import (
    get_caps_from_pad,
    get_numpy_from_buffer,
)

from hailo_apps.python.core.common.hailo_logger import get_logger
from hailo_apps.python.core.gstreamer.gstreamer_app import app_callback_class

hailo_logger = get_logger(__name__)
# endregion imports


# ===============================================================================================
# MIDI Configuration
# ===============================================================================================

MIDI_CONFIG = {
    # Which MIDI port to use (index from available ports, or None for virtual port)
    "port_index": 1,

    # MIDI channel (0-15, i.e. channel 1-16)
    "channel": 0,

    # Note range: maps Y-axis position to these MIDI notes
    # Default: C3 (48) to C5 (72) = 2 octaves
    "note_min": 48,
    "note_max": 72,

    # Fixed velocity for triggered notes
    "velocity": 100,

    # Use a musical scale instead of chromatic?
    # Options: "chromatic", "major", "minor", "pentatonic", "blues"
    "scale": "pentatonic",

    # Root note for scale (0=C, 1=C#, 2=D, ... 11=B)
    "root": 0,

    # --- Two-hand control (all keypoints as seen in the mirrored camera image) ---
    # Pitch hand (left hand in mirrored image = right_wrist keypoint)
    # Y-position controls pitch: hand up = high note, hand down = low note
    "pitch_keypoint": "right_wrist",

    # Trigger hand (right hand in mirrored image = left_wrist keypoint)
    # Hand above shoulder = Note On, below shoulder = Note Off
    "trigger_keypoint": "left_wrist",

    # Shoulder reference for trigger threshold
    # (right shoulder in mirrored image = left_shoulder keypoint)
    "trigger_shoulder": "left_shoulder",

    # Hysteresis offset (normalized, relative to shoulder Y-position)
    # Note On when hand Y < shoulder Y - hysteresis (above shoulder)
    # Note Off when hand Y > shoulder Y + hysteresis (below shoulder)
    "trigger_hysteresis": 0.03,

    # --- Note hysteresis (anti-trilling) ---
    # Minimum Y-position change (normalized) required before switching to a new note.
    # Prevents rapid trilling when the pitch hand is near a note boundary.
    # Increase this value if trilling persists (e.g., 0.03 - 0.05).
    "note_hysteresis": 0.02,

    # --- MOD Wheel (CC#1) ---
    # Controlled by the height of the trigger hand above the shoulder.
    # Hand at shoulder level = MOD 0, hand at top of frame = MOD 127.
    "mod_wheel_enabled": True,
    "mod_wheel_cc": 1,  # MIDI CC number (1 = standard Mod Wheel)

    # Minimum confidence to use a keypoint
    "min_confidence": 0.5,

    # Minimum time between note changes (seconds) to avoid jitter
    "note_cooldown": 0.08,
}

# Scale definitions (intervals from root)
SCALES = {
    "chromatic":   [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
    "major":       [0, 2, 4, 5, 7, 9, 11],
    "minor":       [0, 2, 3, 5, 7, 8, 10],
    "pentatonic":  [0, 2, 4, 7, 9],
    "blues":       [0, 3, 5, 6, 7, 10],
}


def build_note_list(scale_name, root, note_min, note_max):
    """Build a list of MIDI notes within the given range that belong to the scale."""
    intervals = SCALES.get(scale_name, SCALES["chromatic"])
    notes = []
    for midi_note in range(note_min, note_max + 1):
        degree = (midi_note - root) % 12
        if degree in intervals:
            notes.append(midi_note)
    return notes


# ===============================================================================================
# MIDI Controller Class
# ===============================================================================================

class MidiController:
    """Handles MIDI output with two-hand control, note hysteresis, and MOD wheel."""

    def __init__(self, config=None):
        self.config = config or MIDI_CONFIG
        self.midiout = rtmidi.MidiOut()
        self.current_note = None
        self.last_note_time = 0
        self.trigger_active = False
        self.last_pitch_y = None  # Last Y-position that resulted in a note change
        self.last_mod_value = -1  # Track last sent MOD value to avoid redundant messages

        # Build the note list from scale configuration
        self.note_list = build_note_list(
            self.config["scale"],
            self.config["root"],
            self.config["note_min"],
            self.config["note_max"],
        )
        hailo_logger.info(
            "Scale '%s' root=%d: %d notes from %d to %d",
            self.config["scale"],
            self.config["root"],
            len(self.note_list),
            self.note_list[0] if self.note_list else 0,
            self.note_list[-1] if self.note_list else 0,
        )

        # Open MIDI port
        self._open_port()

    def _open_port(self):
        ports = self.midiout.get_ports()
        hailo_logger.info("Available MIDI ports: %s", ports)

        port_index = self.config.get("port_index")
        if ports and port_index is not None and port_index < len(ports):
            self.midiout.open_port(port_index)
            hailo_logger.info("Opened MIDI port: %s", ports[port_index])
        else:
            self.midiout.open_virtual_port("DanceMIDI")
            hailo_logger.info("Opened virtual MIDI port: DanceMIDI")

    def position_to_note(self, y_normalized):
        """Map a normalized Y position (0.0 - 1.0) to a note from the scale.

        Y is inverted: top of frame (y=0) = high note, bottom (y=1) = low note.
        """
        if not self.note_list:
            return None
        inverted_y = 1.0 - y_normalized
        index = int(inverted_y * (len(self.note_list) - 1))
        index = max(0, min(index, len(self.note_list) - 1))
        return self.note_list[index]

    def _note_passes_hysteresis(self, pitch_y):
        """Check if the pitch hand moved enough to justify a note change.

        Prevents trilling when the hand hovers at a note boundary.
        """
        if self.last_pitch_y is None:
            return True
        return abs(pitch_y - self.last_pitch_y) >= self.config["note_hysteresis"]

    def update_trigger(self, trigger_y, shoulder_y):
        """Update trigger state based on trigger hand Y relative to shoulder Y.

        Uses hysteresis to prevent flicker at the threshold boundary.
        Returns True if trigger state changed.
        """
        hysteresis = self.config["trigger_hysteresis"]
        old_state = self.trigger_active

        if not self.trigger_active:
            if trigger_y < shoulder_y - hysteresis:
                self.trigger_active = True
        else:
            if trigger_y > shoulder_y + hysteresis:
                self.trigger_active = False

        return self.trigger_active != old_state

    def update_pitch(self, pitch_y):
        """Update the pitch based on pitch hand Y-position.

        Only sends MIDI if trigger is active, note changed, and hysteresis passed.
        """
        now = time.time()
        if now - self.last_note_time < self.config["note_cooldown"]:
            return

        if not self.trigger_active:
            return

        if not self._note_passes_hysteresis(pitch_y):
            return

        new_note = self.position_to_note(pitch_y)
        if new_note is None:
            return

        channel = self.config["channel"]
        velocity = self.config["velocity"]

        if new_note != self.current_note:
            if self.current_note is not None:
                self.midiout.send_message([0x80 | channel, self.current_note, 0])

            self.midiout.send_message([0x90 | channel, new_note, velocity])
            self.current_note = new_note
            self.last_note_time = now
            self.last_pitch_y = pitch_y

    def update_mod_wheel(self, trigger_y, shoulder_y):
        """Send MOD Wheel CC based on trigger hand height above shoulder.

        Only active while trigger is on. Maps shoulder level (MOD=0) to
        top of frame (MOD=127). Sends only when value changes.
        """
        if not self.config.get("mod_wheel_enabled"):
            return

        channel = self.config["channel"]
        cc_num = self.config["mod_wheel_cc"]

        if not self.trigger_active:
            # Reset MOD to 0 when trigger is off
            if self.last_mod_value != 0:
                self.midiout.send_message([0xB0 | channel, cc_num, 0])
                self.last_mod_value = 0
            return

        # Calculate how far above the shoulder the trigger hand is
        # shoulder_y = reference (MOD 0), y=0.0 = top of frame (MOD 127)
        distance_above = shoulder_y - trigger_y  # positive when above shoulder
        if distance_above <= 0:
            mod_value = 0
        else:
            # Normalize: shoulder_y is the max range (hand at top of frame = shoulder_y distance)
            mod_value = int((distance_above / max(shoulder_y, 0.01)) * 127)
            mod_value = max(0, min(127, mod_value))

        if mod_value != self.last_mod_value:
            self.midiout.send_message([0xB0 | channel, cc_num, mod_value])
            self.last_mod_value = mod_value

    def trigger_on(self, pitch_y):
        """Called when trigger transitions from off to on."""
        new_note = self.position_to_note(pitch_y)
        if new_note is None:
            return

        channel = self.config["channel"]
        velocity = self.config["velocity"]

        if self.current_note is not None:
            self.midiout.send_message([0x80 | channel, self.current_note, 0])

        self.midiout.send_message([0x90 | channel, new_note, velocity])
        self.current_note = new_note
        self.last_note_time = time.time()
        self.last_pitch_y = pitch_y

    def trigger_off(self):
        """Called when trigger transitions from on to off."""
        self.all_notes_off()
        self.last_pitch_y = None

    def all_notes_off(self):
        """Send note-off for the current note and reset."""
        if self.current_note is not None:
            channel = self.config["channel"]
            self.midiout.send_message([0x80 | channel, self.current_note, 0])
            self.current_note = None
        # Also reset MOD wheel
        if self.config.get("mod_wheel_enabled") and self.last_mod_value != 0:
            channel = self.config["channel"]
            cc_num = self.config["mod_wheel_cc"]
            self.midiout.send_message([0xB0 | channel, cc_num, 0])
            self.last_mod_value = 0

    def close(self):
        """Clean shutdown."""
        self.all_notes_off()
        self.midiout.close_port()
        hailo_logger.info("MIDI controller closed.")


# ===============================================================================================
# User-defined class to be used in the callback function
# ===============================================================================================

class user_app_callback_class(app_callback_class):
    def __init__(self):
        super().__init__()
        self.midi = MidiController(MIDI_CONFIG)
        hailo_logger.info("MIDI controller initialized.")


# ===============================================================================================
# User-defined callback function
# ===============================================================================================

def app_callback(element, buffer, user_data):
    hailo_logger.debug("Callback triggered. Current frame count=%d", user_data.get_count())

    if buffer is None:
        hailo_logger.warning("Received None buffer.")
        return

    hailo_logger.debug("Processing frame %d", user_data.get_count())
    string_to_print = f"Frame count: {user_data.get_count()}\n"

    pad = element.get_static_pad("src")
    format, width, height = get_caps_from_pad(pad)

    frame = None
    if user_data.use_frame and format and width and height:
        frame = get_numpy_from_buffer(buffer, format, width, height)

    roi = hailo.get_roi_from_buffer(buffer)
    detections = roi.get_objects_typed(hailo.HAILO_DETECTION)

    keypoints = get_keypoints()
    person_found = False

    for detection in detections:
        label = detection.get_label()
        bbox = detection.get_bbox()
        confidence = detection.get_confidence()

        if label == "person" and confidence >= MIDI_CONFIG["min_confidence"]:
            person_found = True
            track_id = 0
            track = detection.get_objects_typed(hailo.HAILO_UNIQUE_ID)
            if len(track) == 1:
                track_id = track[0].get_id()

            string_to_print += (
                f"Detection: ID: {track_id} Label: {label} "
                f"Confidence: {confidence:.2f}\n"
            )

            landmarks = detection.get_objects_typed(hailo.HAILO_LANDMARKS)
            if landmarks:
                points = landmarks[0].get_points()

                # --- Extract keypoints for two-hand control ---
                pitch_kp = MIDI_CONFIG["pitch_keypoint"]
                trigger_kp = MIDI_CONFIG["trigger_keypoint"]
                shoulder_kp = MIDI_CONFIG["trigger_shoulder"]

                pitch_idx = keypoints.get(pitch_kp)
                trigger_idx = keypoints.get(trigger_kp)
                shoulder_idx = keypoints.get(shoulder_kp)

                pitch_point = points[pitch_idx] if pitch_idx is not None else None
                trigger_point = points[trigger_idx] if trigger_idx is not None else None
                shoulder_point = points[shoulder_idx] if shoulder_idx is not None else None

                if pitch_point and trigger_point and shoulder_point:
                    # Normalized coordinates within full frame
                    pitch_y = pitch_point.y() * bbox.height() + bbox.ymin()
                    pitch_x = pitch_point.x() * bbox.width() + bbox.xmin()

                    trigger_y = trigger_point.y() * bbox.height() + bbox.ymin()
                    trigger_x = trigger_point.x() * bbox.width() + bbox.xmin()

                    shoulder_y = shoulder_point.y() * bbox.height() + bbox.ymin()

                    # --- Update trigger state (with hysteresis) ---
                    trigger_changed = user_data.midi.update_trigger(trigger_y, shoulder_y)

                    if trigger_changed:
                        if user_data.midi.trigger_active:
                            user_data.midi.trigger_on(pitch_y)
                        else:
                            user_data.midi.trigger_off()
                    elif user_data.midi.trigger_active:
                        user_data.midi.update_pitch(pitch_y)

                    # --- Update MOD Wheel ---
                    user_data.midi.update_mod_wheel(trigger_y, shoulder_y)

                    # --- Display info ---
                    current_note_name = midi_note_to_name(
                        user_data.midi.position_to_note(pitch_y)
                    )
                    trigger_state = "ON" if user_data.midi.trigger_active else "OFF"
                    mod_val = user_data.midi.last_mod_value

                    string_to_print += (
                        f"  Pitch ({pitch_kp}): y={pitch_y:.3f} "
                        f"-> Note: {current_note_name}\n"
                        f"  Trigger ({trigger_kp}): y={trigger_y:.3f} "
                        f"Shoulder: y={shoulder_y:.3f} "
                        f"-> {trigger_state}  MOD: {mod_val}\n"
                    )

                    # --- Draw on frame ---
                    if user_data.use_frame and frame is not None:
                        # Pitch hand: green circle with note name
                        px_pitch = int(pitch_x * width)
                        py_pitch = int(pitch_y * height)
                        cv2.circle(frame, (px_pitch, py_pitch), 12, (0, 255, 0), -1)
                        cv2.putText(
                            frame, current_note_name,
                            (px_pitch + 15, py_pitch - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2,
                        )

                        # Trigger hand: blue (on) or red (off)
                        px_trigger = int(trigger_x * width)
                        py_trigger = int(trigger_y * height)
                        trigger_color = (255, 128, 0) if user_data.midi.trigger_active else (0, 0, 255)
                        cv2.circle(frame, (px_trigger, py_trigger), 12, trigger_color, -1)
                        cv2.putText(
                            frame, f"{trigger_state} M{mod_val}",
                            (px_trigger + 15, py_trigger - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, trigger_color, 2,
                        )

                        # Shoulder reference: yellow circle
                        py_shoulder = int(shoulder_y * height)
                        cv2.circle(frame, (px_trigger, py_shoulder), 6, (0, 255, 255), -1)

                        # Trigger threshold zone: draw line at shoulder height
                        cv2.line(
                            frame,
                            (px_trigger - 30, py_shoulder),
                            (px_trigger + 30, py_shoulder),
                            (0, 255, 255), 1,
                        )

                # --- Eye keypoints (from original code) ---
                for eye in ["left_eye", "right_eye"]:
                    keypoint_index = keypoints[eye]
                    point = points[keypoint_index]
                    x = int((point.x() * bbox.width() + bbox.xmin()) * width)
                    y = int((point.y() * bbox.height() + bbox.ymin()) * height)
                    if user_data.use_frame and frame is not None:
                        cv2.circle(frame, (x, y), 5, (0, 255, 0), -1)

            # Only process the first detected person for now
            break

    # If no person is detected, silence and reset
    if not person_found:
        user_data.midi.trigger_active = False
        user_data.midi.all_notes_off()

    if user_data.use_frame and frame is not None:
        frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        user_data.set_frame(frame)

    print(string_to_print)
    return


# ===============================================================================================
# Helper functions
# ===============================================================================================

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_note_to_name(note):
    """Convert MIDI note number to human-readable name (e.g., 60 -> C4)."""
    if note is None:
        return "--"
    octave = (note // 12) - 1
    name = NOTE_NAMES[note % 12]
    return f"{name}{octave}"


def get_keypoints():
    return {
        "nose": 0,
        "left_eye": 1,
        "right_eye": 2,
        "left_ear": 3,
        "right_ear": 4,
        "left_shoulder": 5,
        "right_shoulder": 6,
        "left_elbow": 7,
        "right_elbow": 8,
        "left_wrist": 9,
        "right_wrist": 10,
        "left_hip": 11,
        "right_hip": 12,
        "left_knee": 13,
        "right_knee": 14,
        "left_ankle": 15,
        "right_ankle": 16,
    }


def main():
    hailo_logger.info("Starting Pose Estimation MIDI App.")
    user_data = user_app_callback_class()

    try:
        app = GStreamerPoseEstimationApp(app_callback, user_data)
        app.run()
    except KeyboardInterrupt:
        hailo_logger.info("Interrupted by user.")
    finally:
        user_data.midi.close()
        hailo_logger.info("App shut down cleanly.")


if __name__ == "__main__":
    main()

