import numpy as np
import os
from pathlib import Path
from PyQt6.QtWidgets import QMessageBox
import shutil

class Chase_AWG:
    def __init__(self, record_length, sampling_rate_MS_s, options, window, voltage_max_mV, verbose=True):
        self.RECORD_LENGTH = int(record_length)
        self.SR = int(sampling_rate_MS_s * 1e6)   # Sa/s
        self.OUTPUT_RANGE_MV = int(voltage_max_mV)
        self.verbose = verbose
        self.buffer = None
        self.is_started = False
        self.is_card_open = False
        # self.lMaxADC = 32767   # Simulate a 16-bit card by default
        self.share_path = Path(r"\\raspberrypi\Ramdisk_Share")
        # self.waveform_name="waveform.txt"
        self.bits = 9
        self.dac_min = 0
        self.dac_max = 2**self.bits - 1
        self.dac_mid = self.dac_max // 2
        self.local_waveform_path = None
        self.awg_waveform_path = None

        self.waveform_name = options['data_file'] + ".txt"
        self.loop = options['loop']
        self.trigger = options['trigger']

        self.window = window
    
    def open_card(self):
        # check if network share exists
        if not self.share_path:
            raise FileNotFoundError(
                f"[Chase_AWG] Network share {self.share_path} not found."
            )
        self.is_card_open = True
        if self.verbose:
            print(f"[Chase_AWG] Card open. Connnected to AWG share: {self.share_path}")

    def setup_card(self):
        if not self.is_card_open:
            raise RuntimeError("[Chase_AWG] Card not open!")
        if self.verbose:
            print(f"[Chase_AWG] AWG configured: SR={self.SR*1e-6} MSa/s, RL={self.RECORD_LENGTH}, Range=±{self.OUTPUT_RANGE_MV/1000} V")

    def allocate_buffer(self):
        self.buffer = np.zeros(self.RECORD_LENGTH, dtype=np.int16)
        if self.verbose:
            print(f"[Chase_AWG] Allocated buffer of length {self.RECORD_LENGTH} int16 samples.")

    def load_waveform_in_buffer(self, voltage_array):
        '''Stores the buffer and simulates clipping and scaling.'''
        voltage_array = np.asarray(voltage_array)
        if voltage_array.size != self.RECORD_LENGTH:
            raise ValueError("[Chase_AWG] Voltage array does not match buffer length.")
        clipped = np.clip(voltage_array, -1, 1)

        if np.any(voltage_array < -1) or np.any(voltage_array > 1):
            print("[Chase_AWG] Warning: waveform clipped to [-1,1] before scaling.")

        dac_codes = np.round(
            (clipped + 1.0) * 0.5 * self.dac_max
        ).astype(np.int16)
        self.buffer = dac_codes
        
        if self.verbose:
            print("[Chase_AWG] Loaded and scaled waveform into buffer.")

    def write_waveform_to_card(self):
        if not self.is_card_open:
            raise RuntimeError("[Chase_AWG] Card/share not open!")

        if self.buffer is None:
            raise RuntimeError("[Chase_AWG] No waveform loaded in buffer.")

        local_dir = Path("waveforms/generated")
        local_dir.mkdir(parents=True, exist_ok=True)

        self.local_waveform_path = local_dir / self.waveform_name

        # Waveform format: one integer DAC code per line.
        try:
            with open(self.local_waveform_path, "w", encoding="utf-8") as f:
                for sample in self.buffer:
                    f.write(f"{int(sample)}\n")
        except Exception as e:
            QMessageBox.critical(self.window, "Error", f"Error writing local waveform file, {self.waveform_name}: {e}")
            print(f"[Chase_AWG] Error writing local waveform file, {self.waveform_name}: {e}")

        self.awg_waveform_path = self.share_path / self.waveform_name

        tmp_path = self.awg_waveform_path.with_suffix(
            self.awg_waveform_path.suffix + ".tmp"
        )

        print("  share_path:", repr(str(self.share_path)))
        print("  share_path exists:", Path(self.share_path).exists())
        print("  waveform_name:", repr((self.waveform_name)))
        print("  local_waveform_path:", self.local_waveform_path)
        print("  local exists", self.local_waveform_path.exists())
        print("  awg_waveform_path:", self.awg_waveform_path)
        print("  tmp_path:", tmp_path)

        try:
            shutil.copy2(self.local_waveform_path, tmp_path)

            os.replace(tmp_path, self.awg_waveform_path)

            if self.verbose:
                print("[Chase_AWG] Waveform written to card.")
        except:
            QMessageBox.critical(self.window, "Error", f"Error writing to Chase_AWG")

    def _write_command_file(self, commands):
        if not self.is_card_open:
            raise RuntimeError("[Chase_AWG] Card/share not open!")

        command_path = self.share_path / "command.txt"
        lines = list(commands)

        while len(lines) < 31:
            lines.append("")
        try:
            with open(command_path, "w", encoding="utf-8", newline="\n") as f:
                for line in lines:
                    f.write(line + "\n")
        except Exception as e:
            print(f"[Chase_AWG] Error writing command file: {e}")
            raise

        if self.verbose:
            print("[Chase_AWG] command.txt written:")
            for line in commands:
                print("   ", line)

        return command_path


    def output_waveform(self):
        if self.buffer is None:
            raise RuntimeError("[Chase_AWG] No waveform loaded.")
        if self.awg_waveform_path is None or not self.awg_waveform_path.exists():
            raise RuntimeError("[Chase_AWG] Waveform file not written to AWG card/share.")
        
        #
        commands = [
            "stop",
            f"load_wfm {Path(self.waveform_name).name} 255 {self.loop} {self.trigger}",
            "run",
        ]
        self._write_command_file(commands)
      
        self.is_started = True
        if self.verbose:
            print("[Chase_AWG] Output started. Card is 'outputting'...")


    # def retrigger(self):
    #     if not self.is_started:
    #         print("[Chase_AWG] Cannot retrigger: output not started.")
    #         return

    #     # Placeholder. Confirm actual trigger command from Chase docs.
    #     commands = [
    #         "trigger",
    #     ]
    #     self._write_command_file(commands)

    #     if self.is_started:
    #         if self.verbose:
    #             print("[Chase_AWG] Retrigger command sent.")
    #     else:
    #         print("[Chase_AWG] Cannot retrigger: output not started.")

    def stop_output(self):
        if not self.is_card_open:
            raise RuntimeError("[Chase_AWG] Card/share not open!")

        self._write_command_file(["stop"])

        self.is_started = False

        if self.verbose:
            print("[Chase_AWG] Output stopped.")

    def close_card(self):
        if self.is_started:
            self.stop_output()

        self.is_card_open = False
        self.buffer = None

        if self.verbose:
            print("[Chase_AWG] Connection closed.")