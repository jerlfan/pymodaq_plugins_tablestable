from pathlib import Path
import time

import numpy as np
from qtpy import QtCore

from pymodaq_utils.config import GlobalConfig
from pymodaq_utils.utils import ThreadCommand
from pymodaq_data.data import DataToExport, Axis
from pymodaq_data import Q_, ureg
from pymodaq_gui.parameter import Parameter

from pymodaq.control_modules.viewer_utility_classes import DAQ_Viewer_base, comon_parameters, main
from pymodaq.utils.data import DataFromPlugins

from pymodaq_plugins_tablestable.hardware.ghost import TFP1Ghost, ReplayPort
from pymodaq_plugins_tablestable.utils import Config as PluginConfig

config = GlobalConfig()
REPLAY_FILE = Path(__file__).parents[2].joinpath('resources', 'tfp1_replay.bin')
SCAN_TIMEOUT = 5.  # s, much longer than a scan cycle (about 0.7 s for 512 channels)
FREQUENCY_SETTINGS = ('mirror_spacing', 'wavelength', 'scan_amplitude')  # saved in the configuration when changed

class DAQ_1DViewer_TFP1(DAQ_Viewer_base):
    """PyMoDAQ 1D viewer for a TFP-1 tandem Fabry-Perot interferometer (The Table Stable, formerly JRS).

    The plugin reads the photon counts that the control unit of the TFP-1 sends on its RS-232 output for the GHOST
    software (one-way link, 57600 baud, 8N1, see ``hardware/ghost.py``). Nothing is sent to the control unit: the plugin
    cannot change any setting of the interferometer, and GHOST can keep running on a second serial port connected
    through a Y cable.

    Each acquisition returns the first complete scan starting after the request, in counts per channel, so that the
    scans of a point of a map are all taken once the stage has stopped. Averaging over several scans is left to
    PyMoDAQ (Naverage, live averaging). The channels of the shutter windows (reference beam) are divided by the window
    factor, to keep the reference peak visible without crushing the spectrum.The spectrum is plotted on a frequency axis, from -X to +X GHz with X = (c / 2L) * (scan amplitude / wavelength), L being the mirror spacing; these three settings are kept in the user configuration file. With the port set to MOCK, the plugin
    replays a recording shipped with the package, so that it can be used without the instrument.

    Compatible instruments: TFP-1 control units with the GHOST serial output. The TFP-2 should also work if its
    control unit has the same output (untested).
    Tested with: a TFP-1 in tandem mode (512 channels) on a native RS-232 port, PyMoDAQ 5.3.10, Python 3.12, Windows 11.
    Installation: no manufacturer driver is needed, only ``pyserial``. The serial port is set in the configuration file
    of the plugin (``config_tablestable.toml``, section ``[tfp1]``).
    Usage: the plugin only reads the data. The user remains responsible for the alignment and the stabilisation of the
    interferometer, with the control unit and the software of the manufacturer.

    Attributes:
    -----------
    controller: TFP1Ghost
        Wrapper of the serial output of the control unit
    x_axis: Axis
        Channel axis, rebuilt when the number of channels is changed on the control unit
    """
    params = comon_parameters + [
        {'title': 'Serial port:', 'name': 'port', 'type': 'str', 'value': config('tablestable', 'tfp1', 'port'),
         'tip': 'Serial port receiving the GHOST output (COM6...), MOCK to replay a recording'},
        {'title': 'Window factor:', 'name': 'window_factor', 'type': 'float',
         'value': config('tablestable', 'tfp1', 'window_factor'), 'min': 1.,
         'tip': 'The channels of the shutter windows (reference beam) are divided by this factor'},
        {'title': 'Channels:', 'name': 'n_channels', 'type': 'int', 'value': 0, 'readonly': True,
         'tip': 'Number of channels per scan, read from the data'},
        {'title': 'Frequency axis:', 'name': 'frequency', 'type': 'group', 'children': [
            {'title': 'Mirror spacing (mm):', 'name': 'mirror_spacing', 'type': 'float',
             'value': config('tablestable', 'tfp1', 'mirror_spacing'), 'min': 0.001,
             'tip': 'Distance between the mirrors of the interferometer'},
            {'title': 'Wavelength (nm):', 'name': 'wavelength', 'type': 'float',
             'value': config('tablestable', 'tfp1', 'wavelength'), 'min': 1.},
            {'title': 'Scan amplitude (nm):', 'name': 'scan_amplitude', 'type': 'float',
             'value': config('tablestable', 'tfp1', 'scan_amplitude'), 'min': 0.001},
            {'title': 'Half range (GHz):', 'name': 'half_range', 'type': 'float', 'value': 0., 'readonly': True,
             'tip': 'The spectrum goes from -half range to +half range: (c / 2 spacing) * (amplitude / wavelength)'},
        ]},
    ]
    

    def ini_attributes(self):
        self.controller: TFP1Ghost = None
        self.x_axis: Axis = None
        self._grab_start = 0.
        self._timer = QtCore.QTimer()  # checks every 50 ms whether a scan is complete, during an acquisition
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._check_scan)

    def ini_detector(self, controller=None):
        """Detector communication initialization

        Parameters
        ----------
        controller: (object)
            custom object of a PyMoDAQ plugin (Slave case). None if only one actuator/detector by controller
            (Master case)

        Returns
        -------
        info: str
        initialized: bool
            False if initialization failed otherwise True
        """

        if self.is_master:
            port = ReplayPort(REPLAY_FILE) if self.settings['port'] == 'MOCK' else self.settings['port']
            self.controller = TFP1Ghost(port)
        else:
            self.controller = controller

        try:
            scan = self.controller.wait_scan(timeout=5.)
        except TimeoutError as e:
            if self.is_master:  # free the port, so that a new initialization can open it again
                self.controller.close()
                self.controller = None
            return str(e), False

        self.update_axis(scan.n_channels)
        self.dte_signal_temp.emit(DataToExport('TFP1', data=[
            DataFromPlugins(name='TFP1', data=[np.zeros(scan.n_channels)], dim='Data1D',
                            labels=['Spectrum'], units='count', axes=[self.x_axis])]))
        return f'{scan.n_channels} channels, {"tandem" if scan.tandem else "alignment"} mode', True

    def close(self):
        """Terminate the communication protocol"""
        # only the master owns the controller, and ini_detector may have failed before creating it
        if self.is_master and self.controller is not None:
            self.controller.close()

    def update_axis(self, n_channels: int):
        """Build the frequency axis, from -X to +X GHz with X = (c / 2 spacing) * (amplitude / wavelength)

        The zero frequency is in the middle of the scan (channel n_channels / 2), and the frequency increases with the
        channel number.
        """
        frequency = self.settings.child('frequency')
        half_range = (ureg.speed_of_light / (2 * Q_(frequency['mirror_spacing'], 'mm'))
                      * Q_(frequency['scan_amplitude'], 'nm') / Q_(frequency['wavelength'], 'nm')).to('GHz').magnitude
        frequency.child('half_range').setValue(half_range)
        self.settings.child('n_channels').setValue(n_channels)
        self.x_axis = Axis('Frequency', units='GHz', index=0,
                           data=(np.arange(n_channels) - n_channels / 2) * 2 * half_range / n_channels)
        
    def grab_data(self, Naverage=1, **kwargs):
        """Start a grab from the detector

        Parameters
        ----------
        Naverage: int
            Number of hardware averaging (if hardware averaging is possible, self.hardware_averaging should be set to
            True in class preamble and you should code this implementation)
        kwargs: dict
            others optionals arguments
        """
        self.controller.restart()  # the scan returned will be the first one starting after this call
        self._grab_start = time.monotonic()
        self._timer.start()

    def _check_scan(self):
        """Called by the timer during an acquisition: emit the first complete scan, or give up after a timeout"""
        scans = self.controller.read_scans()
        if scans:
            self._timer.stop()
            self._emit_scan(scans[0])
        elif time.monotonic() - self._grab_start > SCAN_TIMEOUT:
            self._timer.stop()
            self.emit_status(ThreadCommand('Update_Status',
                                           [f'No complete scan within {SCAN_TIMEOUT} s: is the TFP-1 scanning?']))

    def _emit_scan(self, scan):
        """Emit one scan as a spectrum, the channels of the shutter windows divided by the window factor"""
        if scan.n_channels != self.x_axis.size:  # the number of channels was changed on the control unit
            self.update_axis(scan.n_channels)
        spectrum = scan.counts.astype(float)
        spectrum[scan.shutter] /= self.settings['window_factor']
        self.dte_signal.emit(DataToExport('TFP1', data=[
            DataFromPlugins(name='TFP1', data=[spectrum], dim='Data1D', labels=['Spectrum'], units='count',
                            axes=[self.x_axis])]))

    def stop(self):
        """Stop the current acquisition: nothing to tell the control unit, which keeps scanning"""
        self._timer.stop()
        return ''

    def commit_settings(self, param: Parameter):
        """Apply the consequences of a change of value in the detector settings

        Parameters
        ----------
        param: Parameter
            A given parameter (within detector_settings) whose value has been changed by the user
        """
        # the window factor is read at each scan, nothing to do for it
        if param.name() in FREQUENCY_SETTINGS:
            plugin_config = PluginConfig()
            plugin_config['tfp1', param.name()] = param.value()
            plugin_config.save()  # in the user configuration file, so that the value is kept for the next session
            if self.x_axis is not None:
                self.update_axis(self.x_axis.size)  # used from the next scan on
        elif param.name() == 'port':
            self.emit_status(ThreadCommand('Update_Status', ['The new serial port will be used at the next '
                                                             'initialization']))


if __name__ == '__main__':
    main(__file__)
