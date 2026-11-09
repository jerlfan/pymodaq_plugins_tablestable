pymodaq_plugins_tablestable
###########################

.. image:: https://img.shields.io/pypi/v/pymodaq_plugins_tablestable.svg
   :target: https://pypi.org/project/pymodaq_plugins_tablestable/
   :alt: Latest Version

.. image:: https://github.com/jerlfan/pymodaq_plugins_tablestable/actions/workflows/tests.yml/badge.svg
   :target: https://github.com/jerlfan/pymodaq_plugins_tablestable/actions/workflows/tests.yml
   :alt: Tests

PyMoDAQ plugins for instruments of The Table Stable Ltd. (formerly JRS Scientific Instruments). The TFP-1 tandem
Fabry-Perot interferometer is read through the serial output of its control unit (GHOST protocol), so that Brillouin
spectra can be acquired, accumulated and mapped within PyMoDAQ.

This branch targets **PyMoDAQ 5.3.x** (Python 3.10 to 3.13).

Authors
=======

* Jérémie Margueritat (jeremie.margueritat@univ-lyon1.fr)

Instruments
===========

Viewer1D
++++++++

* **TFP1**: Brillouin spectra of a TFP-1 tandem Fabry-Perot interferometer, read from the GHOST serial output of
  its control unit

Installation instructions
=========================

* PyMoDAQ 5.3.x on Windows.
* No manufacturer driver is needed: the plugin reads the RS-232 output of the TFP-1 control unit (57600 baud, 8N1)
  with ``pyserial``.
* The link is one-way: the control unit only sends data, so the plugin cannot change any setting of the
  interferometer. The GHOST software can keep running on a second serial port connected through a Y cable.

Development
===========

* Install the plugin in editable mode: ``pip install -e .``
* Check it, without any hardware: ``check_plugin`` (and ``pytest``)
* If you use an AI coding assistant, point it to ``AGENTS.md``.
