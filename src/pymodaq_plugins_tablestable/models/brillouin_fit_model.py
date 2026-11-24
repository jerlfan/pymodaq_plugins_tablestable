import numpy as np
from scipy.optimize import curve_fit

from pymodaq.extensions.data_mixer.model import DataMixerModel
from pymodaq_data.data import DataToExport, DataCalculated
from pymodaq_gui.parameter import Parameter


def two_lorentzians(x, offset, nu, gamma, a_neg, a_pos):
    """Symmetric Brillouin doublet on a constant background.

    offset: background | nu: Brillouin shift | gamma: common HWHM
    a_neg, a_pos: amplitudes of the peaks at -nu and +nu
    """
    gamma = abs(gamma)
    return (offset
            + a_neg * gamma**2 / ((x + nu)**2 + gamma**2)
            + a_pos * gamma**2 / ((x - nu)**2 + gamma**2))


class DataMixerBrillouinFitModel(DataMixerModel):
    params = [
        {'title': 'Get Data:', 'name': 'get_data', 'type': 'bool_push', 'value': False,
         'label': 'Get Data'},
        {'title': 'Data1D:', 'name': 'data1D', 'type': 'itemselect',
         'value': dict(all_items=[], selected=[])},
        {'title': 'Central mask half-width:', 'name': 'mask_halfwidth', 'type': 'float',
         'value': 2.0, 'min': 0.},
        {'title': 'Initial HWHM gamma0:', 'name': 'gamma0', 'type': 'float', 'value': 0.3,
         'min': 0.},
        {'title': 'Export spectrum and fit:', 'name': 'export_spectrum', 'type': 'bool',
         'value': True},
    ]

    def ini_model(self):
        self.show_data_list()

    def show_data_list(self):
        dte = self.modules_manager.probe_detectors()
        self.settings.child('data1D').setValue(
            dict(all_items=dte.get_full_names('data1D'), selected=[]))

    def update_settings(self, param: Parameter):
        if param.name() == 'get_data':
            self.show_data_list()

    def get_guess(self, x: np.ndarray, y: np.ndarray):
        """Initial parameters estimated on the masked spectrum."""
        offset = float(np.median(y))
        amp = float(np.max(y) - offset)
        nu0 = abs(float(x[int(np.argmax(y))]))
        return offset, nu0, self.settings['gamma0'], amp, amp

    def process_dte(self, dte: DataToExport):
        dte_out = DataToExport('BrillouinFit')
        if not self.settings['data1D']['selected']:
            return dte_out

        dwa = dte.get_data_from_full_name(self.settings['data1D']['selected'][0])
        dwa.create_missing_axes()
        axis = dwa.axes[0]
        x = axis.get_data()
        y = dwa.data[0]
        units = axis.units

        mask = np.abs(x) > self.settings['mask_halfwidth']
        try:
            popt, pcov = curve_fit(two_lorentzians, x[mask], y[mask],
                                   p0=self.get_guess(x[mask], y[mask]),
                                   sigma=np.sqrt(y[mask] + 1))  # Poisson weighting
            perr = np.sqrt(np.diag(pcov))
            if not np.all(np.isfinite(perr)):  # degenerate fit (e.g. empty spectrum)
                raise RuntimeError('Covariance could not be estimated')
            nu, fwhm = abs(popt[1]), 2 * abs(popt[2])
            dnu, dfwhm = perr[1], 2 * perr[2]
            y_fit = two_lorentzians(x, *popt)
        except (RuntimeError, ValueError):  # no convergence or invalid data
            nu = fwhm = dnu = dfwhm = np.nan
            y_fit = np.full_like(y, np.nan, dtype=float)

        dte_out.append(DataCalculated('Shift', data=[np.atleast_1d(nu)],
                                      errors=[np.atleast_1d(dnu)], units=units))
        dte_out.append(DataCalculated('FWHM', data=[np.atleast_1d(fwhm)],
                                      errors=[np.atleast_1d(dfwhm)], units=units))

        if self.settings['export_spectrum']:
            dte_out.append(DataCalculated('Spectrum', data=[y, y_fit],
                                          labels=['data', 'fit'],
                                          axes=[axis.copy()]))
        return dte_out
