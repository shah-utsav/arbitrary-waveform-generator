import math
import random
import numpy as np
import matplotlib.pyplot as plt

class Pulse_Shaper_Calculations:
    def __init__(self, SR, RL):
        self.SR = SR # sampling rate
        self.RL = RL # record length
        self.T = None # pulse length
        self.t = None # time array
        self.wt = None # optical frequency array
        
        # Immediately build arrays on creation and create 1:1 calibration
        self.build_array()
        self.calibration([0, 1, 0])

        # Phase Shaping
        self.f0 = 150 # Scalar, frequency in MHz 
        self.phi_car = np.zeros_like(self.t)
        self.set_carrier_freq_phi(self.f0) 

        self.phi = np.zeros_like(self.t)
        self.phi_eq = np.zeros_like(self.t)    # Array, same length as self.t
        self.phi_tot = np.zeros_like(self.t)

        # Amplitude Shaping
        self.amp_control = 1.0 # Scalar, between 0 and 1
        self.re = np.ones_like(self.t)
        self.im = np.zeros_like(self.t)
        self.amp_eq = np.ones_like(self.t)    # Array, same length as self.t

        # Waveform
        self.Vt = np.zeros_like(self.t)
                
    def build_array(self):
        self.T = self.RL/self.SR
        self.t = np.arange(0,self.RL) / self.SR
        return self.t, self.T

    def calibration(self,b):
        self.wt = np.zeros_like(self.t)
        for ii in range(len(b)):
            self.wt += b[ii] * self.t**ii
        return self.wt
    
    # ======================== Phase Shaping ==========================
    def set_carrier_freq_phi(self,f0):
        self.f0 = f0
        self.phi_car = 2*math.pi*f0*self.t       
    
    class PhiFcn:
        """Phase Functions: Class containing various methods for writing phase masks including: constant, taylor series, and two-color double-pulse"""
        @staticmethod
        def constant(parent):
            parent.phi = np.zeros_like(parent.wt)
            return parent.phi
        
        @staticmethod
        def taylor_series(parent, w0, phi_n):
            parent.phi = np.zeros_like(parent.wt)
            for ii in range(len(phi_n)):
                parent.phi += phi_n[ii]/math.factorial(ii) * (parent.wt-w0)**ii
            return parent.phi
        
        @staticmethod
        def two_color_double_pulse(parent):
            return

    def set_phi_eq(self):
        """Phase Equalizer: Method to load an array of equal length to the record length to use for arbitrary phase shaping"""
        return
    
    def get_total_phi(self):
        """Total phase: carrier + mask + equalizer"""
        self.phi_tot = self.phi_car + self.phi + self.phi_eq
        return self.phi_tot

    # ======================== Amplitude Shaping ==========================
    def set_amp_control(self,A):
        if not (0 <= A <= 1):
            raise ValueError(f"Amplitude control value ({A}) must be between 0 and 1.")
        self.amp_control = A
        #A_clipped = np.clip(A, 0, 1)
        #if np.any(A != A_clipped):
        #    print(f"!!!WARNING: Amplitude control must be than 1, value was clipped to [0, 1]!!!")
        #self.amp_control = A_clipped

    class AmpFcn:   
        """Amplitude Functions: Class containing various methods for writing amplitude masks including: constant, gaussian, delayed pulse, single-color double-pulse, and multiple gaussians""" 
        @staticmethod
        def constant(parent):
            parent.re = np.ones_like(parent.wt)
            parent.im = np.zeros_like(parent.wt)
            return parent.re, parent.im
        
        @staticmethod
        def gaussian(parent,w0,FWHM_w):
            parent.re = np.exp(-4 * np.log(2) * (parent.wt - w0) ** 2 / FWHM_w ** 2)
            parent.im = np.zeros_like(parent.wt)
            return parent.re, parent.im
        
        @staticmethod
        def delayed_pulse(parent,w0,tau,phi):
            """Return a delayed pulse with central locking frequency w0, delay tau, and relative phase phi"""
            arg = tau*(parent.wt-w0)+phi
            parent.re = np.cos(arg)
            parent.im = np.sin(arg)
            return parent.re, parent.im
        
        @staticmethod
        def double_pulse(parent,R,w0,tau,phi):
            arg = tau*(parent.wt-w0)+phi
            parent.re = 0.5*(1+R*np.cos(arg))
            parent.im = 0.5*R*np.sin(arg)
            return parent.re, parent.im
        
        @staticmethod
        def multi_gaussian(parent,w0_list,FWHM_w):
            parent.re = np.zeros_like(parent.wt)
            for w0 in w0_list:
                parent.re += np.exp(-4 * np.log(2) * (parent.wt - w0) ** 2 / FWHM_w ** 2)
            parent.im = np.zeros_like(parent.wt)
            return parent.re, parent.im

    def set_amp_eq(self):
        """Amplitude Equalizer: Method to load an array of equal length to the record length to use for arbitrary amplitude shaping"""
        return
      
    def get_total_amp(self):
        """Total amplitude: control * mask * equalizer"""
        self.re_tot = self.amp_control * self.re * self.amp_eq
        self.im_tot = self.amp_control * self.im * self.amp_eq
        return self.re_tot, self.im_tot

    # ======================== Generate Waveform ==========================

    def generate_waveform(self,randomize_phi=False):
        if randomize_phi:
            rphi = random.uniform(0, 2 * math.pi)
        else:
            rphi = 0
        print(rphi)
        self.phi_tot = self.get_total_phi() + rphi
        self.re_tot, self.im_tot = self.get_total_amp()
        self.Vt = self.re_tot * np.cos(self.phi_tot) - self.im_tot * np.sin(self.phi_tot)
        return self.Vt

    # ======================== Calculate Instaneous Frequencies ==========================
    def instantaneous_freq(self,domain):
        TWO_PI = 2*math.pi
        # In Acoustic Time
        freq_car = np.gradient(self.phi_car, self.t)/TWO_PI
        freq_fcn = np.gradient(self.phi, self.t)/TWO_PI
        freq_eq  = np.gradient(self.phi_eq, self.t)/TWO_PI
        freq_tot  = np.gradient(self.phi_tot, self.t)/TWO_PI

        # In Optical Frequency
        if domain == 'freq':
            dw_dt = np.gradient(self.wt,self.t)/TWO_PI

            dphi_car_dw = freq_car/dw_dt 
            dphi_fcn_dw = freq_fcn/dw_dt
            dphi_eq_dw  = freq_eq/dw_dt
            dphi_tot_dw = freq_tot/dw_dt
            return dphi_car_dw, dphi_fcn_dw, dphi_eq_dw, dphi_tot_dw
        else:
            return freq_car, freq_fcn, freq_eq, freq_tot

    # ======================== Plot Results ==========================
    def plot_pulse_shaper_results(self,domain):

        if domain == 'time':
            x_arr = self.t
            x_tit = "Time"
            x_lab = "t ($\mu$s)"
            x_val = "t"
            y_lab = "d$\phi$("+x_val+")/d"+x_val+"/2/$\pi$ (MHz)"
        elif domain == 'freq':
            x_arr = self.wt
            x_tit = "Optical Freq."
            x_lab = "$\omega$ (rads/fs)"
            x_val = "$\omega$"
            y_lab = "d$\phi$("+x_val+")/d"+x_val+" (fs)"

        fig, axs = plt.subplots(2, 2, figsize=(8,8))

        # Row 0: Phase
        axs[0,0].plot(x_arr, self.phi_car, label='Carrier')
        axs[0,0].plot(x_arr, self.phi, label='Mask')
        axs[0,0].plot(x_arr, self.phi_eq, label='Eq')
        axs[0,0].plot(x_arr, self.phi_tot, label='Total')
        axs[0,0].set_title("Phase vs. " + x_tit)
        axs[0,0].set_xlabel(x_lab)
        axs[0,0].set_ylabel("$\phi$("+x_val+") (rads)")
        axs[0,0].legend()

        # Row 0, col 1: Amplitude
        axs[0,1].plot(x_arr, self.re, label='Real Mask')
        axs[0,1].plot(x_arr, self.re, label='Imag Mask')
        axs[0,1].plot(x_arr, self.amp_eq, label='Eq')
        axs[0,1].plot(x_arr, self.re_tot, label='Real Total')
        axs[0,1].plot(x_arr, self.im_tot, label='Imag Total')
        axs[0,1].set_title("Amplitude vs. " + x_tit)
        axs[0,1].set_xlabel(x_lab)
        axs[0,1].set_ylabel("A("+x_val+") (0-1)")
        axs[0,1].legend()


        freq_car, freq_fcn, freq_eq, freq_tot= self.instantaneous_freq(domain)
        # Row 1, col 0: d(Phase)/dt
        axs[1,0].plot(x_arr, freq_car, label='Carrier')
        axs[1,0].plot(x_arr, freq_fcn, label='Mask')
        axs[1,0].plot(x_arr, freq_eq, label='Eq')
        axs[1,0].plot(x_arr, freq_tot, label='Total')
        axs[1,0].set_title("Derivative of Total Phase vs. " + x_tit)
        axs[1,0].set_xlabel(x_lab)
        axs[1,0].set_ylabel(y_lab)
        axs[1,0].legend()

        # Row 1, col 1: Voltage
        axs[1,1].plot(self.t, self.Vt, color='blue')
        axs[1,1].set_title("Voltage vs. Time")
        axs[1,1].set_xlabel("t ($\mu$s)")
        axs[1,1].set_ylabel("V(t) (-1-1)")

        plt.tight_layout()
        plt.show()    

    # ======================== Corrections ==========================
    def power_linearization(self):
        return
    
    def freq_filter(self):
        return
    
    def freq_correction(self):
        return