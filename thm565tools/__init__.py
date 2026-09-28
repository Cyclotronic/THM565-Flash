"""Tools for talking to Tektronix THM5xx ("TekMeter") instruments over RS-232.

Reimplements the parts of the vendor's 1994 DOS QLOADER.EXE that were
reverse-engineered by disassembly and live serial capture against a real
THM565 (see the PROTOCOL.md writeup and capture.log in the THM500/ reference
folder). Two tools share the same low-level protocol module:

- flash.py: firmware reload, config identity burn, calibration load/burn.
- calibrate.py: the ADJUST.TXT manual calibration procedure, automated.
"""
