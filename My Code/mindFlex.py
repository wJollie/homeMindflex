import math
import queue
import random
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


BAUD_RATE = 9600
STALE_SECONDS = 3.0

BG = "#101827"
PANEL = "#182438"
TEXT = "#e5edf8"
MUTED = "#9cacc3"
CYAN = "#47d7ed"
PURPLE = "#bb95ff"
GREEN = "#63e6a5"

BAND_NAMES = [
    "Delta", "Theta", "Low alpha", "High alpha",
    "Low beta", "High beta", "Low gamma", "High gamma",
]


def parse_reading(line):
    """Accept the 11-column output from Arduino Brain.readCSV()."""
    try:
        values = [int(value) for value in line.strip().split(",")]
    except ValueError:
        return None

    if len(values) != 11:
        return None

    quality, attention, meditation, *bands = values

    if not (
        0 <= quality <= 200
        and 0 <= attention <= 100
        and 0 <= meditation <= 100
        and all(value >= 0 for value in bands)
    ):
        return None

    return {
        "quality": quality,
        "attention": attention,
        "meditation": meditation,
        "bands": bands,
    }


class SerialReader(threading.Thread):
    """Read serial data without blocking the app window."""

    def __init__(self, port, events):
        super().__init__(daemon=True)
        self.port = port
        self.events = events
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            with serial.Serial(
                self.port,
                BAUD_RATE,
                timeout=0.25,
                write_timeout=0.25,
            ) as connection:
                self.events.put(
                    ("status", f"Connected to {self.port}; waiting for readings.")
                )

                buffer = bytearray()

                while not self.stop_event.is_set():
                    chunk = connection.read(
                        min(connection.in_waiting or 1, 4096)
                    )
                    buffer.extend(chunk)

                    while b"\n" in buffer:
                        raw, _, remainder = buffer.partition(b"\n")
                        buffer = bytearray(remainder)

                        reading = parse_reading(
                            raw.decode("ascii", errors="replace")
                        )

                        if reading is not None:
                            self.events.put(
                                ("reading", (time.monotonic(), reading))
                            )

                    # Recover if the incoming stream never includes a newline.
                    if len(buffer) > 4096:
                        buffer.clear()

        except Exception as error:
            if not self.stop_event.is_set():
                self.events.put(("error", str(error)))


class MindflexApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Mindflex Lab")
        self.root.geometry("1060x780")
        self.root.minsize(850, 680)
        self.root.configure(bg=BG)

        self.events = queue.Queue()
        self.reader = None

        self.mode = tk.StringVar(value="Simulator")
        self.port = tk.StringVar()
        self.status = tk.StringVar(value="Simulator ready.")
        self.sim_attention = tk.DoubleVar(value=50)
        self.sim_meditation = tk.DoubleVar(value=50)
        self.bad_contact = tk.BooleanVar(value=False)

        self.reading = None
        self.last_received = 0.0
        self.next_simulation = 0.0
        self.history = deque(maxlen=120)

        self.score = 0
        self.hold_time = 0.0
        self.target = 65
        self.orb_value = 50.0
        self.last_frame = time.monotonic()

        self.build_ui()
        self.refresh_ports()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.tick()

    def build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            ".", background=PANEL, foreground=TEXT,
            font=("Segoe UI", 10)
        )
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG)
        style.configure("TLabelframe", background=BG)
        style.configure("TLabelframe.Label", background=BG, foreground=TEXT)
        style.configure("TCheckbutton", background=BG)
        style.configure("TNotebook", background=BG)
        style.configure("TNotebook.Tab", padding=(18, 9))
        style.configure(
            "Attention.Horizontal.TProgressbar", background=CYAN
        )
        style.configure(
            "Meditation.Horizontal.TProgressbar", background=PURPLE
        )

        header = ttk.Frame(self.root, padding=16)
        header.pack(fill="x")

        ttk.Label(
            header, text="MINDFLEX LAB",
            font=("Segoe UI", 22, "bold")
        ).pack(anchor="w")

        controls = ttk.Frame(header)
        controls.pack(fill="x", pady=(12, 6))

        ttk.Label(controls, text="Input").pack(side="left")

        mode_box = ttk.Combobox(
            controls, textvariable=self.mode,
            values=["Simulator", "Live headset"],
            state="readonly", width=15
        )
        mode_box.pack(side="left", padx=8)
        mode_box.bind("<<ComboboxSelected>>", self.change_mode)

        self.port_box = ttk.Combobox(
            controls, textvariable=self.port,
            state="readonly", width=12
        )
        self.port_box.pack(side="left", padx=4)

        ttk.Button(
            controls, text="Refresh ports", command=self.refresh_ports
        ).pack(side="left", padx=4)

        ttk.Button(
            controls, text="Connect", command=self.connect
        ).pack(side="left", padx=4)

        ttk.Button(
            controls, text="Disconnect", command=self.disconnect
        ).pack(side="left", padx=4)

        ttk.Label(
            header, textvariable=self.status, wraplength=980
        ).pack(anchor="w", pady=6)

        self.sim_frame = ttk.LabelFrame(
            self.root, text="Simulator controls", padding=12
        )
        self.sim_frame.pack(fill="x", padx=16, pady=(0, 12))

        for label, variable in [
            ("Attention", self.sim_attention),
            ("Meditation", self.sim_meditation),
        ]:
            row = ttk.Frame(self.sim_frame)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=12).pack(side="left")
            ttk.Scale(
                row, from_=0, to=100,
                variable=variable, orient="horizontal"
            ).pack(side="left", fill="x", expand=True)

        ttk.Checkbutton(
            self.sim_frame, text="Simulate poor contact",
            variable=self.bad_contact
        ).pack(anchor="w", pady=(5, 0))

        meters = ttk.Frame(self.root, padding=(16, 0))
        meters.pack(fill="x")

        self.attention_text = tk.StringVar(value="Attention: —")
        self.meditation_text = tk.StringVar(value="Meditation: —")

        for label_var, attribute, meter_style in [
            (
                self.attention_text, "attention_bar",
                "Attention.Horizontal.TProgressbar"
            ),
            (
                self.meditation_text, "meditation_bar",
                "Meditation.Horizontal.TProgressbar"
            ),
        ]:
            row = ttk.Frame(meters)
            row.pack(fill="x", pady=4)
            ttk.Label(
                row, textvariable=label_var, width=20
            ).pack(side="left")
            bar = ttk.Progressbar(
                row, maximum=100, style=meter_style
            )
            bar.pack(side="left", fill="x", expand=True)
            setattr(self, attribute, bar)

        self.quality_text = tk.StringVar(value="No readings yet")
        ttk.Label(
            meters, textvariable=self.quality_text
        ).pack(anchor="w", pady=6)

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=16, pady=12)

        dashboard = ttk.Frame(notebook)
        game_page = ttk.Frame(notebook)
        notebook.add(dashboard, text="Dashboard")
        notebook.add(game_page, text="Orb game")

        ttk.Label(
            dashboard,
            text="Attention = cyan   •   Meditation = purple   "
                 "•   Latest 120 readings",
        ).pack(anchor="w", pady=8)

        self.graph = tk.Canvas(
            dashboard, bg=PANEL, highlightthickness=0
        )
        self.graph.pack(fill="both", expand=True)

        self.bands_text = tk.StringVar(value="Band powers: —")
        ttk.Label(
            dashboard, textvariable=self.bands_text,
            wraplength=950
        ).pack(anchor="w", pady=10)

        ttk.Label(
            game_page,
            text="Raise or lower attention to move the orb. "
                 "Hold inside the target for 3 seconds to score.",
            wraplength=950
        ).pack(anchor="w", pady=8)

        ttk.Button(
            game_page, text="Reset game", command=self.reset_game
        ).pack(anchor="w", pady=(0, 8))

        self.game = tk.Canvas(
            game_page, bg=PANEL, highlightthickness=0
        )
        self.game.pack(fill="both", expand=True)

    def refresh_ports(self):
        ports = (
            [item.device for item in list_ports.comports()]
            if list_ports else []
        )
        self.port_box["values"] = ports

        if self.port.get() not in ports:
            self.port.set(ports[0] if ports else "")

    def stop_reader(self):
        if self.reader:
            self.reader.stop()
            self.reader.join(timeout=1)
            self.reader = None

        # Discard events from the previous connection.
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break

    def clear_input(self):
        self.reading = None
        self.last_received = 0.0
        self.history.clear()
        self.hold_time = 0.0
        self.attention_text.set("Attention: —")
        self.meditation_text.set("Meditation: —")
        self.attention_bar["value"] = 0
        self.meditation_bar["value"] = 0
        self.bands_text.set("Band powers: —")

    def change_mode(self, event=None):
        self.stop_reader()
        self.clear_input()

        if self.mode.get() == "Simulator":
            self.next_simulation = 0
            self.status.set("Simulator ready. Use the sliders.")
        else:
            self.status.set("Choose the Nano's port, then click Connect.")

    def connect(self):
        if self.mode.get() != "Live headset":
            self.status.set("Select Live headset before connecting.")
            return

        if serial is None:
            self.status.set(
                "Install pyserial: py -m pip install pyserial"
            )
            return

        if not self.port.get():
            self.status.set("No port selected. Plug in the Nano and refresh.")
            return

        self.stop_reader()
        self.clear_input()
        self.status.set("Opening serial port...")
        self.reader = SerialReader(self.port.get(), self.events)
        self.reader.start()

    def disconnect(self):
        self.stop_reader()
        self.clear_input()
        self.status.set(
            "Simulator ready."
            if self.mode.get() == "Simulator"
            else "Disconnected."
        )

    def accept_reading(self, timestamp, reading):
        self.last_received = timestamp
        self.reading = reading
        self.history.append(
            (reading["attention"], reading["meditation"],
             reading["quality"])
        )

        self.attention_text.set(
            f"Attention: {reading['attention']}"
        )
        self.meditation_text.set(
            f"Meditation: {reading['meditation']}"
        )
        self.attention_bar["value"] = reading["attention"]
        self.meditation_bar["value"] = reading["meditation"]

        self.bands_text.set(
            "Band powers: " + "   |   ".join(
                f"{name}: {value:,}"
                for name, value in zip(BAND_NAMES, reading["bands"])
            )
        )

    def input_state(self, now):
        if self.reading is None:
            return False, "Waiting for readings"

        if now - self.last_received > STALE_SECONDS:
            return False, "Readings stopped — game paused"

        quality = self.reading["quality"]

        if quality != 0:
            return False, f"Check headset contact — quality {quality}"

        # NeuroSky uses zero as an unavailable eSense value.
        if self.reading["attention"] == 0:
            return False, "Attention unavailable — game paused"

        return True, "Good contact"

    def reset_game(self):
        self.score = 0
        self.hold_time = 0.0
        self.target = 65

    def draw_graph(self):
        canvas = self.graph
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()

        if width < 60 or height < 60:
            return

        left, top = 38, 18
        right, bottom = width - 16, height - 25

        for value in range(0, 101, 25):
            y = bottom - value / 100 * (bottom - top)
            canvas.create_line(left, y, right, y, fill="#2b3a50")
            canvas.create_text(
                left - 8, y, text=str(value),
                fill=MUTED, anchor="e"
            )

        samples = list(self.history)

        # Break the lines at poor-contact samples.
        for column, color in [(0, CYAN), (1, PURPLE)]:
            points = []

            for index, sample in enumerate(samples):
                if sample[2] != 0:
                    if len(points) >= 4:
                        canvas.create_line(
                            *points, fill=color, width=2
                        )
                    points = []
                    continue

                x = left + index / 119 * (right - left)
                y = bottom - sample[column] / 100 * (bottom - top)
                points.extend([x, y])

            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=2)

    def draw_game(self, valid, reason):
        canvas = self.game
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()

        if width < 100 or height < 100:
            return

        top, bottom = 65, height - 45
        travel = max(1, bottom - top)

        def y_for(value):
            return bottom - value / 100 * travel

        center_x = width * 0.5
        target_top = y_for(self.target + 10)
        target_bottom = y_for(self.target - 10)

        canvas.create_rectangle(
            center_x - 100, target_top,
            center_x + 100, target_bottom,
            fill="#203f3b", outline=GREEN, width=2
        )
        canvas.create_text(
            center_x + 115, y_for(self.target),
            text=f"Target: {self.target} ± 10",
            fill=GREEN, anchor="w"
        )

        orb_y = y_for(self.orb_value)
        canvas.create_oval(
            center_x - 20, orb_y - 20,
            center_x + 20, orb_y + 20,
            fill=CYAN if valid else MUTED, outline=""
        )

        canvas.create_text(
            18, 20, anchor="w", fill=TEXT,
            font=("Segoe UI", 14, "bold"),
            text=f"Score: {self.score}    Hold: {self.hold_time:.1f} / 3.0 s"
        )

        canvas.create_text(
            width - 18, 20, anchor="e",
            fill=PURPLE, text=self.mode.get().upper()
        )

        if not valid:
            canvas.create_text(
                width / 2, height - 18,
                text=reason, fill="#ffcf80"
            )

    def tick(self):
        now = time.monotonic()
        dt = min(now - self.last_frame, 0.1)
        self.last_frame = now

        if self.mode.get() == "Simulator":
            if now >= self.next_simulation:
                self.next_simulation = now + 1.0

                reading = {
                    "quality": 200 if self.bad_contact.get() else 0,
                    "attention": round(self.sim_attention.get()),
                    "meditation": round(self.sim_meditation.get()),
                    # Synthetic values for display testing only.
                    "bands": [
                        random.randint(100, 10000) for _ in range(8)
                    ],
                }
                self.accept_reading(now, reading)

        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "reading":
                timestamp, reading = payload
                self.accept_reading(timestamp, reading)
                self.status.set(
                    f"Receiving from {self.port.get()} at {BAUD_RATE} baud."
                )
            elif kind == "error":
                self.status.set(f"Connection error: {payload}")
                self.last_received = 0.0
            else:
                self.status.set(payload)

        valid, reason = self.input_state(now)
        self.quality_text.set(
            f"{self.mode.get()} • {reason}"
        )

        if valid:
            attention = self.reading["attention"]

            # Smooth animation while preserving the original meter reading.
            smoothing = 1 - math.exp(-dt / 0.6)
            self.orb_value += (
                attention - self.orb_value
            ) * smoothing

            if abs(self.orb_value - self.target) <= 10:
                self.hold_time += dt

                if self.hold_time >= 3:
                    self.score += 1
                    self.hold_time = 0
                    choices = [
                        value for value in range(20, 81, 10)
                        if abs(value - self.target) >= 20
                    ]
                    self.target = random.choice(choices)
            else:
                self.hold_time = 0
        else:
            self.hold_time = 0

        self.draw_graph()
        self.draw_game(valid, reason)
        self.root.after(33, self.tick)

    def close(self):
        self.stop_reader()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = MindflexApp(root)
    root.mainloop()