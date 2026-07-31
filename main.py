import sys
import numpy as np
from hardware.AWG._spectrumAWG import *

if __name__ == "__main__":
    #============= SET USER PARAMETERS ============= 
    T = 10 # waveform pulse length (microsecond)
    SR = 1250 # sampling rate (MSa/s -> Mega-samples per second)
    F0 = 100 # Radio Frequency (MHz -> MegaHertz)

    RL = np.ceil(SR*T/32)*32
    """ To calculate the record length (RL), take your sample rate (SR) multiplied by
    the time duration (T), divide by 32, round up to the next whole number using np.ceil,
    and multiply by 32 to ensure it matches the 32-sample block size required by digitizer
    cards like the Spectrum Instrumentation M4i series."""

    #============= Start the Spectrum AWG =============
    awg = Spectrum_AWG(RL, SR, max_voltage_mV = 2000)
    