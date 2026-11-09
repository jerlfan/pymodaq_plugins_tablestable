from pathlib import Path
import time

import numpy as np
from qtpy import QtCore

from pymodaq_utils.config import GlobalConfig
from pymodaq_utils.utils import ThreadCommand
from pymodaq_data.data import DataToExport, Axis
from pymodaq_gui.parameter import Parameter

from pymodaq.control_modules.viewer_utility_classes import DAQ_Viewer_base, comon_parameters, main
from pymodaq.utils.data import DataFromPlugins

from pymodaq_plugins_tablestable.hardware.ghost import TFP1Ghost, ReplayPort

config = GlobalConfig()
REPLAY_FILE = Path(__file__).parents[2].joinpath('resources', 'tfp1_replay.bin')
SCAN_TIMEOUT = 5.  # s, much longer than a scan cycle (about 0.7 s for 512 channels)


class DAQ_1DViewer_TFP1(DAQ_Viewer_base):
    """ Instrument plugin class for a 1D viewer.
    
    This object inherits all functionalities to communicate with PyMoDAQ’s DAQ_Viewer module through inheritance via
    DAQ_Viewer_base. It makes a bridge between the DAQ_Viewer module and the Python wrapper of a particular instrument.

    TODO Complete the docstring of your plugin with:
        * The set of instruments that should be compatible with this instrument plugin.
        * With which instrument it has actually been tested.
        * The version of PyMoDAQ during the test.
        * The version of the operating system.
        * Installation instructions: what manufacturer’s drivers should be installed to make it run?

    Attributes:
    -----------
    controller: object
        The particular object that allow the communication with the hardware, in general a python wrapper around the
         hardware library.
         
    # TODO add your particular attributes here if any

    """
    params = comon_parameters + [
        {'title': 'Serial port:', 'name': 'port', 'type': 'str', 'value': config('tablestable', 'tfp1', 'port'),
         'tip': 'Serial port receiving the GHOST output (COM6...), MOCK to replay a recording'},
        {'title': 'Window factor:', 'name': 'window_factor', 'type': 'float',
         'value': config('tablestable', 'tfp1', 'window_factor'), 'min': 1.,
         'tip': 'The channels of the shutter windows (reference beam) are divided by this factor'},
        {'title': 'Channels:', 'name': 'n_channels', 'type': 'int', 'value': 0, 'readonly': True,
         'tip': 'Number of channels per scan, read from the data'},
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

        self.settings.child('n_channels').setValue(scan.n_channels)
        self.x_axis = Axis('Channel', units='', data=np.arange(scan.n_channels), index=0)
        self.dte_signal_temp.emit(DataToExport('TFP1', data=[
            DataFromPlugins(name='TFP1', data=[np.zeros(scan.n_channels)], dim='Data1D',
                            labels=['Spectrum'], units='count', axes=[self.x_axis])]))
        return f'{scan.n_channels} channels, {"tandem" if scan.tandem else "alignment"} mode', True

    def close(self):
        """Terminate the communication protocol"""
        # only the master owns the controller, and ini_detector may have failed before creating it
        if self.is_master and self.controller is not None:
            self.controller.close()

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
            self.settings.child('n_channels').setValue(scan.n_channels)
            self.x_axis = Axis('Channel', units='', data=np.arange(scan.n_channels), index=0)
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
        if param.name() == 'port':
            self.emit_status(ThreadCommand('Update_Status', ['The new serial port will be used at the next '
                                                             'initialization']))


if __name__ == '__main__':
    main(__file__)
