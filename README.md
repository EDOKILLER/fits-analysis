# FITS Analysis

*Choose the best noise-reduction and background-subtraction settings for FITS images, side by side* — original Italian name: «Analisi FITS»

A small desktop tool, the first of the Astropy series: for every FITS image in a folder it computes a grid of noise-reduction and background-subtraction variants, shows them next to each other and lets you pick the best one with a click. Your choices are saved to a CSV file together with the comparison grids.

## Features

* Grid of noise-reduction strengths and background models (photutils) for each image
* Click / double-click to choose, keyboard shortcuts, remembered zoom and parameters
* Results in `<FITS folder>/risultati_analisi/`: `valori_scelti.csv` and PNG grids with the chosen cell in red
* Standalone executable for Ubuntu and Windows 11 (PyInstaller)

> **Language note.** The interface is in **Italian** (large, adjustable text); plots, PDF/PNG cards and scientific output are in **English**. Code comments are in Italian. The original Italian guide is [`LEGGIMI.txt`](LEGGIMI.txt).

**Version:** 1.0 (2026-10-29). **Status:** Stable (1.0). Superseded for full workflows by AutoAstroPipe and FITS Lab.

## Quick start

Requirements: Python 3.10+ (tested with Python 3.14 on Ubuntu) with `tkinter` (Ubuntu: `sudo apt install python3-tk`).

```bash
git clone https://github.com/EDOKILLER/fits-analysis.git
cd fits-analysis
python3 -m pip install -r requirements.txt

python3 analisi_fits.py --autotest
python3 analisi_fits.py        # opens the window
```

### Standalone executable

`bash crea_eseguibile_ubuntu.sh` builds a single-file executable with PyInstaller in a temporary virtual environment, runs the self-test, and adds the program with its icon to the application menu and the desktop (first time on GNOME: right-click → *Allow launching*). On Windows 11 install Python from python.org (tick *Add python.exe to PATH*) and double-click `crea_eseguibile_windows.bat`.

The build scripts were written for the author's machine: check the paths at the top before running them.

## Data sources and acknowledgements

The program does not download data from external archives.

This project is independent and not endorsed by any of these institutions.

## License

Code: [BSD 3-Clause](LICENSE) © 2026 Edoardo Battistel. You can use, modify and redistribute it, also commercially, as long as you keep the copyright notice; the author's name may not be used to promote derived works without permission. If you use it in research, see [`CITATION.cff`](CITATION.cff).

---
Part of a series of astronomy programs by Edoardo Battistel — see also [X-rays from Black Holes](https://github.com/EDOKILLER/x-rays-from-black-holes) and the other repositories on [this profile](https://github.com/EDOKILLER?tab=repositories).
