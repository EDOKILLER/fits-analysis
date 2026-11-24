#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ANALISI FITS - noise reduction e sottrazione del fondo
=====================================================
Programma con finestra (Ubuntu e Windows 11) che, per ogni file FITS di una
cartella:
  1) applica la noise reduction gaussiana con diversi valori di sigma e fa
     scegliere all'operatore il migliore (clic sull'immagine);
  2) con la sigma scelta, sottrae il fondo con photutils.Background2D usando
     diverse box size e fa scegliere la migliore;
  3) salva i valori scelti in una tabella CSV e le griglie di confronto in PNG.

Risultati in <cartella dei FITS>/risultati_analisi/:
  - valori_scelti.csv            (separatore ';', si apre con Excel/LibreOffice)
  - <nome>_noise_reduction.png   (griglia con la scelta evidenziata in rosso)
  - <nome>_fondo.png

Uso come eseguibile (senza Python): vedi crea_eseguibile_ubuntu.sh e
crea_eseguibile_windows.bat.

Uso come script:
  pip install numpy scipy matplotlib astropy photutils psutil
  Ubuntu: serve anche Tkinter ->  sudo apt install python3-tk
  python analisi_fits.py

Dimensione del testo: pulsanti A− / A+ in alto a sinistra oppure
Ctrl + / Ctrl − / Ctrl 0. Zoom, parametri e ultima cartella vengono
ricordati al successivo avvio (file ~/.analisi_fits.json).
"""

import csv
import json
import gc
import math
import os
import queue
import shutil
import sys
import threading
import time
import traceback
import inspect
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.ndimage import binary_dilation

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

from astropy.io import fits
from astropy.convolution import Gaussian2DKernel, convolve_fft
from astropy.stats import SigmaClip, sigma_clipped_stats
from astropy.visualization import ImageNormalize, ZScaleInterval, AsinhStretch

from photutils.background import Background2D, MedianBackground
from photutils.segmentation import detect_sources

try:
    import psutil          # facoltativo: controllo della RAM libera reale
except ImportError:
    psutil = None


# =============================================================================
# CONFIGURAZIONE PREDEFINITA (i valori principali si cambiano dalla finestra)
# =============================================================================
SIGMA_PREDEFINITE = "0.5, 1, 2, 3, 5"       # noise reduction: sigma gaussiana (pixel)
BOX_PREDEFINITE = "64, 128, 256, 512"       # fondo: box size di Background2D (pixel)
MODALITA_PREDEFINITA = "ibrido"             # "ram", "disco", "ibrido"
LIMITE_RAM_GB_PREDEFINITO = 4.0
MAX_LATO_VIS_PREDEFINITO = 2000             # lato massimo delle anteprime (pixel)

ESTENSIONI_FITS = {".fits", ".fit", ".fts"}
NOME_CARTELLA_RISULTATI = "risultati_analisi"
NOME_CSV = "valori_scelti.csv"
SEPARATORE_CSV = ";"

# Background2D e maschera delle sorgenti
FILTER_SIZE = 3
SIGMA_CLIP = 3.0
EXCLUDE_PERCENTILE = 50
SOGLIA_SORGENTI = 3.0           # soglia = mediana + SOGLIA_SORGENTI * σ del fondo
NPIXELS = 10                    # pixel connessi minimi per una sorgente
RAGGIO_DILATAZIONE = 5          # allargamento della maschera (pixel)
MARGINE_BOX = 3                 # sovrapposizione dei tasselli del fondo (in box)

# Memoria
FATTORE_PICCO_FFT = 10          # picco RAM della FFT (multipli dell'immagine float32)
FATTORE_PICCO_BKG = 12          # picco RAM di Background2D / maschera
TILE = 2048                     # lato dei tasselli (pixel)
MAX_PIXEL_STATISTICHE = 4_000_000
DTYPE = np.float32

# Visualizzazione
ASINH_A = 0.1
N_COLONNE = 3
CMAP = "gray"
DPI_PNG = 150

# Interfaccia: zoom iniziale (poi si regola con A−/A+ e viene ricordato).
# Su Linux Tk non rileva bene la densità dello schermo e il testo risulta
# piccolo, quindi si parte più grandi.
ZOOM_PREDEFINITO = 1.0 if sys.platform == "win32" else 1.4
ZOOM_MIN, ZOOM_MAX, ZOOM_PASSO = 0.8, 2.6, 0.1
FILE_CONFIG = Path.home() / ".analisi_fits.json"

# photutils 3.0 ha rinominato 'npixels' in 'n_pixels'
_ARG_NPIXELS = ("n_pixels" if "n_pixels" in inspect.signature(detect_sources).parameters
                else "npixels")


# =============================================================================
# MEMORIA
# =============================================================================
def gb(n_byte):
    return n_byte / 1024**3


def ram_libera_byte():
    if psutil is None:
        return float("inf")
    return psutil.virtual_memory().available


def ram_processo_gb():
    if psutil is None:
        return float("nan")
    return gb(psutil.Process().memory_info().rss)


def restituisci_ram_al_sistema():
    """gc + (solo Linux) malloc_trim, che restituisce al sistema la memoria libera."""
    gc.collect()
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except (OSError, AttributeError):
            pass


class Memoria:
    """Decide se un'operazione va in RAM o su disco e gestisce i file temporanei.
    - "ram":    sempre in RAM
    - "disco":  sempre su disco (memmap .npy) con elaborazione a tasselli
    - "ibrido": in RAM finché gli array tenuti + il picco stimato restano sotto
                il limite e sotto la RAM libera reale, altrimenti su disco"""

    def __init__(self, modalita, limite_gb, cartella_base):
        self.modalita = modalita
        self.limite = limite_gb * 1024**3
        self.cartella_base = Path(cartella_base)
        self.cartella = None
        self.tenuti = []          # array in RAM che restano vivi tra un passaggio e l'altro

    def byte_in_ram(self):
        return sum(a.nbytes for a in self.tenuti if not isinstance(a, np.memmap))

    def in_ram(self, byte_picco):
        if self.modalita == "ram":
            return True
        if self.modalita == "disco":
            return False
        return (self.byte_in_ram() + byte_picco <= self.limite
                and byte_picco < ram_libera_byte())

    def tieni(self, a):
        if not isinstance(a, np.memmap):
            self.tenuti.append(a)
        return a

    def rilascia(self, a):
        self.tenuti = [t for t in self.tenuti if t is not a]

    def su_disco(self, nome, shape, dtype=DTYPE):
        if self.cartella is None:
            self.cartella = self.cartella_base / f"_tmp_{os.getpid()}_{int(time.time())}"
            self.cartella.mkdir(parents=True, exist_ok=True)
        return np.lib.format.open_memmap(self.cartella / f"{nome}.npy", mode="w+",
                                         dtype=dtype, shape=shape)

    def chiudi(self):
        """Cancella i file temporanei. Su Windows un file mappato non si può
        cancellare finché è aperto: si riprova dopo il garbage collector."""
        self.tenuti = []
        gc.collect()
        if self.cartella is not None:
            for _ in range(5):
                shutil.rmtree(self.cartella, ignore_errors=True)
                if not self.cartella.exists():
                    break
                gc.collect()
                time.sleep(0.3)
            self.cartella = None


# =============================================================================
# CALCOLO (nessuna dipendenza dalla finestra)
# =============================================================================
def tasselli(shape, lato, margine):
    """(y0, y1, x0, x1, ya, yb, xa, xb): zona utile e zona letta (con margine).
    lato=None -> immagine intera."""
    ny, nx = shape
    if lato is None:
        yield 0, ny, 0, nx, 0, ny, 0, nx
        return
    for y0 in range(0, ny, lato):
        y1 = min(y0 + lato, ny)
        ya, yb = max(y0 - margine, 0), min(y1 + margine, ny)
        for x0 in range(0, nx, lato):
            x1 = min(x0 + lato, nx)
            xa, xb = max(x0 - margine, 0), min(x1 + margine, nx)
            yield y0, y1, x0, x1, ya, yb, xa, xb


def carica_immagine(percorso, ritaglio, mem):
    """Legge l'estensione 'SCI' (o la prima HDU 2D) con memmap=True e la copia
    in RAM oppure, se non c'è spazio, a blocchi in un .npy su disco.
    Il FITS viene chiuso subito (importante su Windows)."""
    with fits.open(percorso, memmap=True) as hdul:
        if "SCI" in hdul:
            hdu = hdul["SCI"]
        else:
            hdu = next((h for h in hdul if h.data is not None and h.data.ndim == 2), None)
            if hdu is None:
                raise ValueError("Nessuna immagine 2D trovata nel file")
        if hdu.data.ndim != 2:
            raise ValueError(f"L'estensione scientifica ha {hdu.data.ndim} dimensioni, ne servono 2")
        header = hdu.header.copy()
        sorgente = hdu.data if ritaglio is None else hdu.data[ritaglio]
        forma = sorgente.shape
        byte_img = forma[0] * forma[1] * np.dtype(DTYPE).itemsize

        if mem.in_ram(byte_img):
            dati = mem.tieni(np.array(sorgente, dtype=DTYPE))
        else:
            dati = mem.su_disco("originale", forma)
            for y0 in range(0, forma[0], TILE):
                dati[y0:y0 + TILE] = sorgente[y0:y0 + TILE]
            dati.flush()
        del sorgente, hdu
    return dati, header


def convolvi_intera(dati, sigma):
    kernel = Gaussian2DKernel(x_stddev=sigma)
    out = convolve_fft(np.asarray(dati), kernel, nan_treatment="interpolate",
                       preserve_nan=True, allow_huge=True)
    return out.astype(DTYPE, copy=False)


def convolvi_a_tasselli(dati, sigma, uscita):
    kernel = Gaussian2DKernel(x_stddev=sigma)
    m = kernel.shape[0] // 2
    for y0, y1, x0, x1, ya, yb, xa, xb in tasselli(dati.shape, TILE, m):
        blocco = np.asarray(dati[ya:yb, xa:xb], dtype=np.float64)
        conv = convolve_fft(blocco, kernel, nan_treatment="interpolate", preserve_nan=True)
        uscita[y0:y1, x0:x1] = conv[y0 - ya:y1 - ya, x0 - xa:x1 - xa]
        del blocco, conv
    uscita.flush()
    return uscita


def riduci_rumore(dati, sigma, mem, nome):
    """Noise reduction in RAM (immagine intera) o su disco (a tasselli)."""
    if sigma <= 0:
        return dati
    byte_img = dati.size * np.dtype(DTYPE).itemsize
    if mem.in_ram(byte_img * FATTORE_PICCO_FFT):
        return convolvi_intera(dati, sigma)
    return convolvi_a_tasselli(dati, sigma, mem.su_disco(nome, dati.shape))


def snr_globale(img):
    """SNR medio globale = media / deviazione standard dei pixel finiti."""
    somma = somma_q = n = 0.0
    for y0 in range(0, img.shape[0], TILE):
        b = np.asarray(img[y0:y0 + TILE], dtype=np.float64)
        b = b[np.isfinite(b)]
        somma += b.sum()
        somma_q += np.dot(b, b)
        n += b.size
    if n == 0:
        return float("nan")
    media = somma / n
    std = math.sqrt(max(somma_q / n - media**2, 0.0))
    return media / std if std > 0 else float("nan")


def passo_campionamento(shape):
    return max(1, math.ceil(math.sqrt(shape[0] * shape[1] / MAX_PIXEL_STATISTICHE)))


def statistiche_fondo(img, maschera=None):
    """Mediana e σ sigma-clipped dei pixel di fondo, su un campione regolare.
    I pixel NaN/inf vengono tolti PRIMA del sigma clipping (niente warning)."""
    p = passo_campionamento(img.shape)
    v = np.asarray(img[::p, ::p], dtype=np.float64)
    validi = np.isfinite(v)
    if maschera is not None:
        validi &= ~np.asarray(maschera[::p, ::p])
    v = v[validi]
    if v.size == 0:
        return float("nan"), float("nan")
    _, mediana, std = sigma_clipped_stats(v, sigma=SIGMA_CLIP)
    return float(mediana), float(std)


def disco(raggio):
    y, x = np.mgrid[-raggio:raggio + 1, -raggio:raggio + 1]
    return x**2 + y**2 <= raggio**2


def crea_maschera_sorgenti(img, soglia, uscita, lato):
    margine = RAGGIO_DILATAZIONE + 64
    struttura = disco(RAGGIO_DILATAZIONE) if RAGGIO_DILATAZIONE > 0 else None
    for y0, y1, x0, x1, ya, yb, xa, xb in tasselli(img.shape, lato, margine):
        blocco = np.asarray(img[ya:yb, xa:xb], dtype=np.float32)
        non_validi = ~np.isfinite(blocco)
        if non_validi.all():
            uscita[y0:y1, x0:x1] = False
            continue
        segm = detect_sources(np.where(non_validi, 0, blocco), soglia,
                              mask=non_validi, **{_ARG_NPIXELS: NPIXELS})
        m = np.zeros(blocco.shape, bool) if segm is None else segm.data > 0
        if struttura is not None:
            m = binary_dilation(m, structure=struttura)
        uscita[y0:y1, x0:x1] = m[y0 - ya:y1 - ya, x0 - xa:x1 - xa]
        del blocco, non_validi, segm, m
    if hasattr(uscita, "flush"):
        uscita.flush()
    return uscita


def sottrai_fondo(img, maschera, box, uscita, lato, fondo_riserva):
    """Scrive (img - fondo Background2D) in 'uscita'. A tasselli multipli della
    box, sovrapposti di MARGINE_BOX box: la griglia coincide con quella
    dell'immagine intera e le giunture non si vedono.
    Restituisce (mediana del fondo stimato, n. tasselli con fondo di riserva)."""
    if lato is not None:
        lato = max(lato // box, 4) * box
    margine = MARGINE_BOX * box
    mediane, falliti = [], 0
    for y0, y1, x0, x1, ya, yb, xa, xb in tasselli(img.shape, lato, margine):
        blocco = np.asarray(img[ya:yb, xa:xb], dtype=np.float32)
        copertura = ~np.isfinite(blocco)
        m = np.asarray(maschera[ya:yb, xa:xb]) & ~copertura
        try:
            bkg = Background2D(blocco, box_size=box, filter_size=FILTER_SIZE,
                               sigma_clip=SigmaClip(sigma=SIGMA_CLIP),
                               bkg_estimator=MedianBackground(),
                               mask=m, coverage_mask=copertura,
                               exclude_percentile=EXCLUDE_PERCENTILE)
            fondo = bkg.background
            mediane.append(float(bkg.background_median))
            del bkg
        except ValueError:
            fondo = fondo_riserva          # tassello tutto NaN o tutto mascherato
            falliti += 1
        uscita[y0:y1, x0:x1] = (blocco - fondo)[y0 - ya:y1 - ya, x0 - xa:x1 - xa]
        del blocco, copertura, m, fondo
    if hasattr(uscita, "flush"):
        uscita.flush()
    return (float(np.median(mediane)) if mediane else float("nan")), falliti


def anteprima(img, max_lato):
    """Copia sottocampionata per lo schermo (1 pixel ogni 'passo', non media,
    così l'aspetto del rumore resta quello reale)."""
    passo = 1 if not max_lato else max(1, math.ceil(max(img.shape) / max_lato))
    return np.array(img[::passo, ::passo], dtype=np.float32)


class AnalisiFile:
    """Stato dell'analisi di un singolo file FITS."""

    def __init__(self, percorso, ritaglio, modalita, limite_gb, cartella_temp, max_lato):
        self.percorso = Path(percorso)
        self.ritaglio = ritaglio
        self.mem = Memoria(modalita, limite_gb, cartella_temp)
        self.max_lato = max_lato
        self.dati = None
        self.header = None
        self.unita = ""
        self.sigma_scelta = None
        self.snr_scelto = None
        self.frazione_maschera = None

    # ---- Fase 1: noise reduction ----
    def fase_noise_reduction(self, sigmas, log):
        log(f"Caricamento di {self.percorso.name} ...")
        self.dati, self.header = carica_immagine(self.percorso, self.ritaglio, self.mem)
        self.unita = self.header.get("BUNIT", "")
        dove = "disco" if isinstance(self.dati, np.memmap) else "RAM"
        log(f"Dimensioni {self.dati.shape}, originale in {dove} | "
            f"filtro {self.header.get('FILTER', 'n/d')} | unità {self.unita or 'n/d'}")

        pannelli = []
        for s in [0.0] + list(sigmas):
            # stesso nome file per tutte le prove: su disco resta una sola copia alla volta
            img = riduci_rumore(self.dati, s, self.mem, "nr_prova")
            snr = snr_globale(img)
            titolo = "Nessuna noise reduction" if s == 0 else f"Noise reduction σ = {s:g} px"
            pannelli.append({"valore": s, "snr": snr, "vista": anteprima(img, self.max_lato),
                             "titolo": f"{titolo}\nSNR medio = {snr:.4f}"})
            log(f"  σ = {s:g} px -> SNR medio = {snr:.4f} "
                f"[{'disco' if isinstance(img, np.memmap) else 'RAM'}] "
                f"| RAM processo {ram_processo_gb():.2f} GB")
            del img                       # serve solo l'anteprima: si libera subito
            gc.collect()
        return pannelli

    # ---- Fase 2: sottrazione del fondo ----
    def fase_fondo(self, sigma, snr, boxes, log):
        self.sigma_scelta, self.snr_scelto = sigma, snr
        log(f"Noise reduction con σ = {sigma:g} px ...")
        nr = riduci_rumore(self.dati, sigma, self.mem, "nr_scelta")
        if nr is not self.dati:
            self.mem.tieni(nr)
            self.mem.rilascia(self.dati)
            self.dati = None              # l'originale non serve più
            gc.collect()
        forma = nr.shape
        byte_img = nr.size * np.dtype(DTYPE).itemsize

        p = passo_campionamento(forma)
        campione = np.asarray(nr[::p, ::p], dtype=np.float64)
        campione = campione[np.isfinite(campione)]      # niente NaN nel sigma clipping
        _, mediana_glob, std_glob = sigma_clipped_stats(campione, sigma=SIGMA_CLIP)
        del campione
        soglia = mediana_glob + SOGLIA_SORGENTI * std_glob
        log(f"Fondo globale: mediana = {mediana_glob:.4g}, σ = {std_glob:.4g} {self.unita}")

        if self.mem.in_ram(byte_img * FATTORE_PICCO_BKG):
            maschera = crea_maschera_sorgenti(nr, soglia, np.empty(forma, bool), None)
            self.mem.tieni(maschera)
        else:
            maschera = crea_maschera_sorgenti(nr, soglia, self.mem.su_disco("maschera", forma, bool), TILE)
        self.frazione_maschera = sum(np.count_nonzero(maschera[y:y + TILE])
                                     for y in range(0, forma[0], TILE)) / (forma[0] * forma[1])
        log(f"Maschera sorgenti: {100 * self.frazione_maschera:.1f}% dei pixel")
        if self.frazione_maschera > 0.6:
            log("ATTENZIONE: maschera molto estesa, il fondo sarà stimato su pochi pixel.")

        pannelli = []
        med, std = statistiche_fondo(nr, maschera)
        snr_rif = snr_globale(nr)
        pannelli.append({"valore": 0, "snr": snr_rif, "residuo": (med, std),
                         "vista": anteprima(nr, self.max_lato),
                         "titolo": f"Fondo NON sottratto\nSNR medio = {snr_rif:.4f}\n"
                                   f"Residuo: med = {med:.3g}, σ = {std:.3g}"})
        log(f"  senza sottrazione -> residuo mediana = {med:.4g}, σ = {std:.4g}")

        for box in boxes:
            if self.mem.in_ram(byte_img * FATTORE_PICCO_BKG):
                img = np.empty(forma, DTYPE)
                fondo_med, falliti = sottrai_fondo(nr, maschera, box, img, None, mediana_glob)
            else:
                img = self.mem.su_disco("fondo_prova", forma)
                fondo_med, falliti = sottrai_fondo(nr, maschera, box, img, TILE, mediana_glob)
            snr_b = snr_globale(img)
            med, std = statistiche_fondo(img, maschera)
            pannelli.append({"valore": box, "snr": snr_b, "residuo": (med, std),
                             "vista": anteprima(img, self.max_lato),
                             "titolo": f"Fondo sottratto: box = {box} px\nSNR medio = {snr_b:.4f}\n"
                                       f"Residuo: med = {med:.3g}, σ = {std:.3g}"})
            avviso = f" ({falliti} tasselli con fondo globale)" if falliti else ""
            log(f"  box = {box} px -> fondo ≈ {fondo_med:.4g}, residuo mediana = {med:.4g}, "
                f"σ = {std:.4g}{avviso} | RAM processo {ram_processo_gb():.2f} GB")
            del img
            gc.collect()

        vista_maschera = anteprima(maschera, self.max_lato).astype(bool)
        del nr, maschera
        self.dati = None                  # (se σ = 0, nr era l'originale)
        self.mem.chiudi()                # libera RAM e file temporanei: restano le anteprime
        restituisci_ram_al_sistema()
        return pannelli, vista_maschera

    def chiudi(self):
        self.dati = None
        self.mem.chiudi()
        restituisci_ram_al_sistema()


# =============================================================================
# TABELLA CSV
# =============================================================================
COLONNE_CSV = ["file", "data_analisi", "strumento", "filtro", "ritaglio",
               "sigma_noise_reduction_px", "snr_medio", "box_fondo_px",
               "fondo_residuo_mediana", "fondo_residuo_sigma", "unita"]


def leggi_csv(percorso):
    if not percorso.exists():
        return {}
    with open(percorso, newline="", encoding="utf-8-sig") as f:
        return {r["file"]: r for r in csv.DictReader(f, delimiter=SEPARATORE_CSV)}


def scrivi_csv(percorso, righe):
    """Riscrive tutta la tabella (una riga per file: un file rianalizzato viene aggiornato).
    utf-8-sig: Excel su Windows riconosce correttamente gli accenti."""
    tmp = percorso.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLONNE_CSV, delimiter=SEPARATORE_CSV,
                           extrasaction="ignore")
        w.writeheader()
        for nome in sorted(righe):
            w.writerow(righe[nome])
    os.replace(tmp, percorso)


# =============================================================================
# IMPOSTAZIONI UTENTE (zoom, parametri, ultima cartella)
# =============================================================================
def carica_config():
    try:
        with open(FILE_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def salva_config(cfg):
    try:
        with open(FILE_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


# =============================================================================
# FINESTRA
# =============================================================================
def leggi_lista(testo, tipo, minimo):
    valori = []
    for parte in testo.replace(";", ",").replace(" ", ",").split(","):
        if parte.strip():
            v = tipo(float(parte.strip()))
            if v < minimo:
                raise ValueError(f"valore {v} minore di {minimo}")
            valori.append(v)
    if not valori:
        raise ValueError("lista vuota")
    return sorted(set(valori))


def leggi_ritaglio(testo):
    """'2000:4000, 3000:5000' -> (slice(2000, 4000), slice(3000, 5000)); vuoto -> None."""
    testo = testo.strip()
    if not testo:
        return None
    parti = [p.strip() for p in testo.split(",")]
    if len(parti) != 2:
        raise ValueError("formato: y0:y1, x0:x1")
    fette = []
    for p in parti:
        a, b = p.split(":")
        fette.append(slice(int(a) if a.strip() else None, int(b) if b.strip() else None))
    return tuple(fette)


class App(tk.Tk):
    FONT_DI_SISTEMA = ("TkDefaultFont", "TkTextFont", "TkFixedFont", "TkMenuFont",
                       "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont",
                       "TkIconFont", "TkTooltipFont")

    def __init__(self):
        super().__init__()
        self.title("Analisi FITS - noise reduction e fondo")
        self.geometry("1366x800")
        self.minsize(900, 560)
        try:
            self.state("zoomed")                  # Windows
        except tk.TclError:
            try:
                self.attributes("-zoomed", True)  # Linux
            except tk.TclError:
                pass

        self.cfg = carica_config()
        self.zoom = min(max(float(self.cfg.get("zoom", ZOOM_PREDEFINITO)), ZOOM_MIN), ZOOM_MAX)

        self.cartella = None
        self.file_fits = []
        self.righe_csv = {}
        self.analisi = None
        self.stato = "inattivo"       # inattivo | lavoro | scelta_nr | scelta_fondo
        self.coda = queue.Queue()
        self.assi_valori = {}
        self.scelta = None
        self.indice = 0
        self._ultimo_disegno = None   # per ridisegnare le immagini quando cambia lo zoom

        self._prepara_font()
        self._costruisci_interfaccia()
        self._applica_zoom()
        self._scorciatoie()
        self.protocol("WM_DELETE_WINDOW", self._chiudi_finestra)
        self.after(100, self._controlla_coda)
        self._aggiorna_pulsanti()

    # ------------------------------------------------------------------ font/zoom
    def _prepara_font(self):
        """Memorizza la dimensione originale dei font di sistema: lo zoom la moltiplica."""
        self._font_base = {}
        for nome in self.FONT_DI_SISTEMA:
            try:
                f = tkfont.nametofont(nome)
            except tk.TclError:
                continue
            self._font_base[nome] = f.cget("size") or 10
        base = tkfont.nametofont("TkDefaultFont").actual()
        self.font_titolo = tkfont.Font(family=base["family"], weight="bold")
        self.font_pulsante = tkfont.Font(family=base["family"], weight="bold")
        self.font_registro = tkfont.Font(family=tkfont.nametofont("TkFixedFont").actual()["family"])
        self.style = ttk.Style(self)
        # il tema 'clam' permette barre di scorrimento larghe sia su Linux sia su Windows
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        # i menu a tendina delle combobox usano un widget Tk classico
        self.option_add("*TCombobox*Listbox.font", "TkDefaultFont")

    @staticmethod
    def _scala(dimensione, fattore):
        """Le dimensioni Tk negative sono in pixel, positive in punti: si conserva il segno."""
        nuova = max(1, round(abs(dimensione) * fattore))
        return -nuova if dimensione < 0 else nuova

    def _applica_zoom(self):
        z = self.zoom
        for nome, base in self._font_base.items():
            tkfont.nametofont(nome).configure(size=self._scala(base, z))
        base = self._font_base.get("TkDefaultFont", 10)
        self.font_titolo.configure(size=self._scala(base, 1.25 * z))
        self.font_pulsante.configure(size=self._scala(base, 1.1 * z))
        self.font_registro.configure(size=self._scala(self._font_base.get("TkFixedFont", base), 0.95 * z))

        p = lambda v: max(1, round(v * z))
        self.style.configure("TButton", padding=(p(8), p(4)))
        self.style.configure("Grande.TButton", font=self.font_pulsante, padding=(p(12), p(7)))
        self.style.configure("TEntry", padding=p(3))
        self.style.configure("TCombobox", padding=p(3))
        self.style.configure("TCheckbutton", padding=p(2))
        self.style.configure("TLabelframe.Label", font=self.font_pulsante)
        spessore = p(22)                       # barre di scorrimento grandi
        for orient in ("Vertical", "Horizontal"):
            self.style.configure(f"{orient}.TScrollbar", arrowsize=spessore, width=spessore)
        self.style.configure("TProgressbar", thickness=p(16))
        self.lbl_cartella.configure(wraplength=p(320))
        self.lbl_zoom.configure(text=f"Testo {round(z * 100)}%")
        self.progresso.configure(length=p(200))

        # ridisegna le immagini con i titoli alla nuova dimensione (solo durante una scelta)
        if self._ultimo_disegno is not None and self.stato in ("scelta_nr", "scelta_fondo"):
            scelta = self.scelta["valore"] if self.scelta is not None else None
            self._disegna(*self._ultimo_disegno)
            if scelta is not None:
                for ax, pannello in self.assi_valori.items():
                    if pannello["valore"] == scelta:
                        self._evidenzia(ax)

    def _cambia_zoom(self, delta=None):
        if delta is None:
            self.zoom = ZOOM_PREDEFINITO
        else:
            self.zoom = min(max(round(self.zoom + delta, 2), ZOOM_MIN), ZOOM_MAX)
        self._applica_zoom()
        self.cfg["zoom"] = self.zoom
        salva_config(self.cfg)
        return "break"

    def _scorciatoie(self):
        for tasto in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
            self.bind_all(tasto, lambda e: self._cambia_zoom(+ZOOM_PASSO))
        for tasto in ("<Control-minus>", "<Control-KP_Subtract>"):
            self.bind_all(tasto, lambda e: self._cambia_zoom(-ZOOM_PASSO))
        self.bind_all("<Control-0>", lambda e: self._cambia_zoom(None))
        self.bind_all("<Return>", lambda e: self._conferma() if self.scelta is not None else None)

    # ------------------------------------------------------------------ layout
    def _costruisci_interfaccia(self):
        principale = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        principale.pack(fill=tk.BOTH, expand=True)

        # ---- colonna sinistra ----
        sx = ttk.Frame(principale, padding=8)
        principale.add(sx, weight=0)

        fr_zoom = ttk.Frame(sx)
        fr_zoom.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(fr_zoom, text="A−", width=4, command=lambda: self._cambia_zoom(-ZOOM_PASSO)).pack(side=tk.LEFT)
        ttk.Button(fr_zoom, text="A+", width=4, command=lambda: self._cambia_zoom(+ZOOM_PASSO)).pack(side=tk.LEFT, padx=(4, 0))
        self.lbl_zoom = ttk.Label(fr_zoom, text="")
        self.lbl_zoom.pack(side=tk.LEFT, padx=8)

        ttk.Button(sx, text="📁 Scegli cartella FITS...", style="Grande.TButton",
                   command=self._scegli_cartella).pack(fill=tk.X)
        self.lbl_cartella = ttk.Label(sx, text="Nessuna cartella", foreground="gray")
        self.lbl_cartella.pack(fill=tk.X, pady=(2, 6))

        ttk.Label(sx, text="File FITS (✓ = già analizzato):").pack(anchor="w")
        fr_lista = ttk.Frame(sx)
        fr_lista.pack(fill=tk.BOTH, expand=True)
        self.lista = tk.Listbox(fr_lista, width=36, height=6, exportselection=False,
                                font="TkDefaultFont", activestyle="dotbox")
        sb = ttk.Scrollbar(fr_lista, orient=tk.VERTICAL, command=self.lista.yview)
        self.lista.configure(yscrollcommand=sb.set)
        self.lista.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.lista.bind("<Button-4>", lambda e: self.lista.yview_scroll(-3, "units"))
        self.lista.bind("<Button-5>", lambda e: self.lista.yview_scroll(3, "units"))
        self.lista.bind("<Double-Button-1>", lambda e: self._analizza_selezionato()
                        if self.stato == "inattivo" else None)

        par = ttk.LabelFrame(sx, text="Parametri", padding=6)
        par.pack(fill=tk.X, pady=6)
        c = self.cfg
        self.var_sigma = tk.StringVar(value=c.get("sigma", SIGMA_PREDEFINITE))
        self.var_box = tk.StringVar(value=c.get("box", BOX_PREDEFINITE))
        self.var_modalita = tk.StringVar(value=c.get("modalita", MODALITA_PREDEFINITA))
        self.var_limite = tk.StringVar(value=str(c.get("limite_ram_gb", LIMITE_RAM_GB_PREDEFINITO)))
        self.var_ritaglio = tk.StringVar(value="")
        self.var_maxlato = tk.StringVar(value=str(c.get("max_lato", MAX_LATO_VIS_PREDEFINITO)))
        self.var_auto = tk.BooleanVar(value=bool(c.get("auto", True)))
        campi = [("Sigma NR (px)*", ttk.Entry(par, textvariable=self.var_sigma, width=16)),
                 ("Box fondo (px)*", ttk.Entry(par, textvariable=self.var_box, width=16)),
                 ("Memoria", ttk.Combobox(par, textvariable=self.var_modalita, state="readonly",
                                          values=["ram", "disco", "ibrido"], width=14)),
                 ("Limite RAM (GB)", ttk.Entry(par, textvariable=self.var_limite, width=16)),
                 ("Ritaglio y0:y1, x0:x1", ttk.Entry(par, textvariable=self.var_ritaglio, width=16)),
                 ("Lato anteprime (px)", ttk.Entry(par, textvariable=self.var_maxlato, width=16))]
        for i, (etichetta, widget) in enumerate(campi):
            ttk.Label(par, text=etichetta).grid(row=i, column=0, sticky="w", pady=2)
            widget.grid(row=i, column=1, sticky="ew", pady=2, padx=(6, 0))
        par.columnconfigure(1, weight=1)
        ttk.Label(par, text="* separati da virgola, decimali col punto",
                  foreground="gray").grid(row=len(campi), column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(par, text="Passa da solo al file successivo",
                        variable=self.var_auto).grid(row=len(campi) + 1, column=0, columnspan=2, sticky="w")

        self.btn_analizza = ttk.Button(sx, text="▶  Analizza file selezionato", style="Grande.TButton",
                                       command=self._analizza_selezionato)
        self.btn_analizza.pack(fill=tk.X)
        self.btn_salta = ttk.Button(sx, text="Salta / annulla file", command=self._salta_file)
        self.btn_salta.pack(fill=tk.X, pady=(4, 0))

        ttk.Label(sx, text="Registro:").pack(anchor="w", pady=(6, 0))
        fr_reg = ttk.Frame(sx)
        fr_reg.pack(fill=tk.BOTH, expand=True)
        self.registro = tk.Text(fr_reg, width=36, height=5, wrap="word", font=self.font_registro)
        sb2 = ttk.Scrollbar(fr_reg, orient=tk.VERTICAL, command=self.registro.yview)
        self.registro.configure(yscrollcommand=sb2.set)
        self.registro.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb2.pack(side=tk.RIGHT, fill=tk.Y)

        # ---- area immagini ----
        dx = ttk.Frame(principale, padding=8)
        principale.add(dx, weight=1)
        self.lbl_fase = ttk.Label(dx, text="Scegli una cartella con file FITS per iniziare.",
                                  font=self.font_titolo)
        self.lbl_fase.pack(anchor="w", fill=tk.X)
        dx.bind("<Configure>", lambda e: self.lbl_fase.configure(wraplength=max(200, e.width - 20)))

        self.fig = Figure(layout="constrained")
        self.canvas = FigureCanvasTkAgg(self.fig, master=dx)
        barra = ttk.Frame(dx)
        barra.pack(fill=tk.X)
        self.toolbar = NavigationToolbar2Tk(self.canvas, barra, pack_toolbar=False)
        self.toolbar.update()
        self.toolbar.pack(side=tk.LEFT)

        # in basso prima della figura: con la finestra piccola si restringe la figura, non i pulsanti
        basso = ttk.Frame(dx)
        basso.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        self.lbl_scelta = ttk.Label(basso, text="", font=self.font_pulsante)
        self.lbl_scelta.pack(side=tk.LEFT)
        self.btn_conferma = ttk.Button(basso, text="✔  Conferma scelta", style="Grande.TButton",
                                       command=self._conferma)
        self.btn_conferma.pack(side=tk.RIGHT)
        self.progresso = ttk.Progressbar(basso, mode="indeterminate", length=200)
        self.progresso.pack(side=tk.RIGHT, padx=8)

        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.canvas.mpl_connect("button_press_event", self._clic_immagine)

    # ------------------------------------------------------------- utilità GUI
    def _log(self, testo):
        self.registro.insert(tk.END, testo + "\n")
        self.registro.see(tk.END)

    def _aggiorna_pulsanti(self):
        occupato = self.stato == "lavoro"
        self.btn_analizza.configure(state=tk.NORMAL if self.stato == "inattivo" and self.file_fits else tk.DISABLED)
        self.btn_salta.configure(state=tk.NORMAL if self.stato in ("scelta_nr", "scelta_fondo") else tk.DISABLED)
        self.btn_conferma.configure(state=tk.NORMAL if self.scelta is not None and not occupato else tk.DISABLED)
        if occupato:
            self.progresso.start(12)
        else:
            self.progresso.stop()

    def _cartella_risultati(self):
        d = self.cartella / NOME_CARTELLA_RISULTATI
        d.mkdir(exist_ok=True)
        return d

    # ------------------------------------------------------------ cartella/lista
    def _scegli_cartella(self):
        if self.stato != "inattivo":
            messagebox.showinfo("Analisi in corso", "Termina o annulla il file in corso prima di cambiare cartella.")
            return
        iniziale = self.cfg.get("ultima_cartella")
        if not iniziale or not Path(iniziale).is_dir():
            iniziale = str(Path.home())
        d = filedialog.askdirectory(title="Cartella con i file FITS", initialdir=iniziale)
        if not d:
            return
        self.cartella = Path(d)
        self.cfg["ultima_cartella"] = str(self.cartella)
        salva_config(self.cfg)
        self.file_fits = sorted(p for p in self.cartella.iterdir()
                                if p.is_file() and p.suffix.lower() in ESTENSIONI_FITS)
        self.lbl_cartella.configure(text=str(self.cartella), foreground="black")
        self.righe_csv = leggi_csv(self.cartella / NOME_CARTELLA_RISULTATI / NOME_CSV)
        self._riempi_lista()
        self._log(f"Cartella: {self.cartella} ({len(self.file_fits)} file FITS)")
        if not self.file_fits:
            messagebox.showwarning("Nessun file", "Nella cartella non ci sono file .fits/.fit/.fts")
        else:
            self._seleziona_prossimo(0)
        self._aggiorna_pulsanti()

    def _riempi_lista(self):
        sel = self.lista.curselection()
        self.lista.delete(0, tk.END)
        for p in self.file_fits:
            r = self.righe_csv.get(p.name)
            if r:
                testo = f"✓ {p.name}  (σ={r['sigma_noise_reduction_px']}, box={r['box_fondo_px']})"
            else:
                testo = f"   {p.name}"
            self.lista.insert(tk.END, testo)
        if sel:
            self.lista.selection_set(sel[0])

    def _seleziona_prossimo(self, da):
        """Seleziona il primo file non ancora analizzato a partire dall'indice 'da'."""
        n = len(self.file_fits)
        for k in list(range(da, n)) + list(range(0, da)):
            if self.file_fits[k].name not in self.righe_csv:
                break
        else:
            k = min(da, n - 1)
        self.lista.selection_clear(0, tk.END)
        self.lista.selection_set(k)
        self.lista.see(k)
        return k

    # -------------------------------------------------------------- lavoro
    def _in_background(self, funzione, al_termine):
        """Esegue i calcoli in un thread separato: la finestra resta reattiva.
        Il disegno avviene solo nel thread principale (al_termine)."""
        self.stato = "lavoro"
        self.scelta = None
        self._aggiorna_pulsanti()

        def corpo():
            try:
                risultato = funzione(lambda msg: self.coda.put(("log", msg)))
                self.coda.put(("fatto", (al_termine, risultato)))
            except MemoryError:
                self.coda.put(("errore", "Memoria esaurita. Prova la modalità 'disco' o 'ibrido' "
                                         "con un limite più basso, oppure un ritaglio."))
            except Exception:
                self.coda.put(("errore", traceback.format_exc()))

        threading.Thread(target=corpo, daemon=True).start()

    def _controlla_coda(self):
        try:
            while True:
                tipo, dato = self.coda.get_nowait()
                if tipo == "log":
                    self._log(dato)
                elif tipo == "fatto":
                    al_termine, risultato = dato
                    try:
                        al_termine(risultato)
                    except Exception:
                        self.coda.put(("errore", traceback.format_exc()))
                elif tipo == "errore":
                    self._log("ERRORE:\n" + dato)
                    messagebox.showerror("Errore", dato.strip().splitlines()[-1])
                    self._termina_file(salvato=False)
        except queue.Empty:
            pass
        self.after(100, self._controlla_coda)

    def _leggi_parametri(self):
        try:
            sigmas = leggi_lista(self.var_sigma.get(), float, 0.01)
            boxes = leggi_lista(self.var_box.get(), int, 8)
            limite = float(self.var_limite.get().replace(",", "."))
            ritaglio = leggi_ritaglio(self.var_ritaglio.get())
            max_lato = int(self.var_maxlato.get()) if self.var_maxlato.get().strip() else None
        except (ValueError, TypeError) as e:
            messagebox.showerror("Parametri non validi", str(e))
            return None
        # i parametri validi vengono ricordati per il prossimo avvio
        self.cfg.update({"sigma": self.var_sigma.get(), "box": self.var_box.get(),
                         "modalita": self.var_modalita.get(), "limite_ram_gb": limite,
                         "max_lato": max_lato or "", "auto": self.var_auto.get()})
        salva_config(self.cfg)
        return sigmas, boxes, limite, ritaglio, max_lato

    def _analizza_selezionato(self):
        sel = self.lista.curselection()
        if not sel:
            messagebox.showinfo("Nessun file", "Seleziona un file dalla lista.")
            return
        parametri = self._leggi_parametri()
        if parametri is None:
            return
        sigmas, boxes, limite, ritaglio, max_lato = parametri
        self.indice = sel[0]
        self.boxes = boxes
        percorso = self.file_fits[self.indice]
        if percorso.name in self.righe_csv and not messagebox.askyesno(
                "Già analizzato", f"{percorso.name} è già nella tabella. Rianalizzarlo?"):
            return
        self.analisi = AnalisiFile(percorso, ritaglio, self.var_modalita.get(), limite,
                                   self._cartella_risultati(), max_lato)
        self._log(f"\n=== {percorso.name} ({self.indice + 1}/{len(self.file_fits)}) "
                  f"| memoria: {self.var_modalita.get()} ===")
        self.lbl_fase.configure(text=f"{percorso.name} - calcolo della noise reduction ...")
        self._in_background(lambda log: self.analisi.fase_noise_reduction(sigmas, log),
                            self._mostra_noise_reduction)

    def _mostra_noise_reduction(self, pannelli):
        self.stato = "scelta_nr"
        nome = self.analisi.percorso.name
        self._disegna(pannelli, f"{nome} - noise reduction")
        self.lbl_fase.configure(text=f"Fase 1/2 - {nome}: clicca sull'immagine con la noise "
                                     f"reduction migliore, poi Conferma")
        self._aggiorna_pulsanti()

    def _mostra_fondo(self, risultato):
        pannelli, vista_maschera = risultato
        self.stato = "scelta_fondo"
        nome = self.analisi.percorso.name
        self._disegna(pannelli, f"{nome} - sottrazione del fondo "
                                f"(noise reduction σ = {self.analisi.sigma_scelta:g} px)",
                      maschera=vista_maschera)
        self.lbl_fase.configure(text=f"Fase 2/2 - {nome}: clicca sull'immagine con il fondo "
                                     f"migliore (o su 'Fondo NON sottratto'), poi Conferma")
        self._aggiorna_pulsanti()

    # -------------------------------------------------------------- disegno
    def _disegna(self, pannelli, titolo, maschera=None):
        self._ultimo_disegno = (pannelli, titolo, maschera)
        fs = 9 * self.zoom                 # dimensione dei titoli, segue lo zoom
        self.fig.clear()
        self.assi_valori = {}
        n = len(pannelli) + (1 if maschera is not None else 0)
        ncol = min(N_COLONNE, n)
        nrow = math.ceil(n / ncol)
        assi = self.fig.subplots(nrow, ncol, squeeze=False, sharex=True, sharey=True)
        for ax, p in zip(assi.flat, pannelli):
            v = p["vista"]
            finiti = v[np.isfinite(v)]
            vmin, vmax = ZScaleInterval().get_limits(finiti) if finiti.size else (0, 1)
            norm = ImageNormalize(vmin=vmin, vmax=vmax, stretch=AsinhStretch(a=ASINH_A))
            ax.imshow(v, origin="lower", cmap=CMAP, norm=norm, interpolation="nearest")
            ax.set_title(p["titolo"], fontsize=fs)
            ax.set_xticks([]); ax.set_yticks([])
            self.assi_valori[ax] = p
        resto = list(assi.flat[len(pannelli):])
        if maschera is not None:
            ax = resto.pop(0)
            ax.imshow(maschera, origin="lower", cmap="gray", interpolation="nearest")
            ax.set_title(f"Maschera sorgenti (bianco = escluso)\n"
                         f"{100 * self.analisi.frazione_maschera:.1f}% dei pixel", fontsize=fs)
            ax.set_xticks([]); ax.set_yticks([])
        for ax in resto:
            ax.axis("off")
        self.fig.suptitle(titolo, fontsize=fs * 1.25, fontweight="bold")
        self.scelta = None
        self.lbl_scelta.configure(text="Clicca sull'immagine migliore "
                                       "(doppio clic o Invio = conferma)", foreground="black")
        self.canvas.draw_idle()

    def _evidenzia(self, ax_scelto):
        """Bordo rosso sull'immagine scelta e aggiorna la scritta in basso."""
        for ax in self.assi_valori:
            for sp in ax.spines.values():
                sp.set_edgecolor("black"); sp.set_linewidth(0.8)
        for sp in ax_scelto.spines.values():
            sp.set_edgecolor("red"); sp.set_linewidth(5)
        self.scelta = self.assi_valori[ax_scelto]
        v = self.scelta["valore"]
        if self.stato == "scelta_nr":
            testo = "nessuna noise reduction" if v == 0 else f"σ = {v:g} px"
        else:
            testo = "nessuna sottrazione del fondo" if v == 0 else f"box = {v} px"
        self.lbl_scelta.configure(text=f"Scelta: {testo}", foreground="red")
        self.canvas.draw_idle()
        self._aggiorna_pulsanti()

    def _clic_immagine(self, evento):
        if self.stato not in ("scelta_nr", "scelta_fondo") or evento.inaxes not in self.assi_valori:
            return
        modo = getattr(self.toolbar.mode, "value", self.toolbar.mode)
        if modo:                       # zoom o pan attivi: il clic non sceglie
            return
        self._evidenzia(evento.inaxes)
        if evento.dblclick:            # doppio clic = scegli e conferma
            self.after(50, self._conferma)

    # ------------------------------------------------------------ conferma
    def _salva_png(self, suffisso):
        percorso = self._cartella_risultati() / f"{self.analisi.percorso.stem}_{suffisso}.png"
        self.fig.savefig(percorso, dpi=DPI_PNG)
        self._log(f"Salvata griglia: {percorso.name}")

    def _conferma(self):
        if self.scelta is None:
            return
        if self.stato == "scelta_nr":
            self._salva_png("noise_reduction")
            sigma, snr = self.scelta["valore"], self.scelta["snr"]
            self._log(f"Scelta noise reduction: σ = {sigma:g} px")
            self.lbl_fase.configure(text=f"{self.analisi.percorso.name} - calcolo della sottrazione del fondo ...")
            boxes = self.boxes
            self._in_background(lambda log: self.analisi.fase_fondo(sigma, snr, boxes, log),
                                self._mostra_fondo)
        elif self.stato == "scelta_fondo":
            self._salva_png("fondo")
            box = self.scelta["valore"]
            med, std = self.scelta["residuo"]
            a = self.analisi
            rit = self.var_ritaglio.get().strip()
            riga = {"file": a.percorso.name,
                    "data_analisi": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "strumento": a.header.get("INSTRUME", ""),
                    "filtro": a.header.get("FILTER", ""),
                    "ritaglio": rit,
                    "sigma_noise_reduction_px": f"{a.sigma_scelta:g}",
                    "snr_medio": f"{self.scelta['snr']:.6g}",
                    "box_fondo_px": str(box),
                    "fondo_residuo_mediana": f"{med:.6g}",
                    "fondo_residuo_sigma": f"{std:.6g}",
                    "unita": a.unita}
            if self._salva_riga(riga):
                self._log(f"Scelta fondo: {'nessuna sottrazione' if box == 0 else f'box = {box} px'} "
                          f"-> salvato in {NOME_CSV}")
                self._termina_file(salvato=True)

    def _salva_riga(self, riga):
        percorso = self._cartella_risultati() / NOME_CSV
        while True:
            try:
                righe = leggi_csv(percorso)
                righe[riga["file"]] = riga
                scrivi_csv(percorso, righe)
                self.righe_csv = righe
                return True
            except PermissionError:
                # Tipico su Windows se il CSV è aperto in Excel
                if not messagebox.askretrycancel(
                        "File in uso", f"Non riesco a scrivere {NOME_CSV}.\n"
                                       f"Se è aperto in Excel chiudilo e premi Riprova."):
                    return False

    def _termina_file(self, salvato):
        if self.analisi is not None:
            self.analisi.chiudi()
            self.analisi = None
        self.stato = "inattivo"
        self.scelta = None
        self._ultimo_disegno = None
        self.lbl_scelta.configure(text="", foreground="black")
        self._riempi_lista()
        self._log(f"RAM processo dopo la pulizia: {ram_processo_gb():.2f} GB")
        self._aggiorna_pulsanti()
        if not salvato:
            self.lbl_fase.configure(text="File non salvato. Seleziona un file e premi Analizza.")
            return
        prossimo = [p for p in self.file_fits if p.name not in self.righe_csv]
        if not prossimo:
            self.lbl_fase.configure(text="Tutti i file della cartella sono stati analizzati.")
            messagebox.showinfo("Finito", f"Tutti i file sono nella tabella:\n"
                                          f"{self._cartella_risultati() / NOME_CSV}")
            return
        self._seleziona_prossimo(self.indice + 1)
        if self.var_auto.get():
            self.after(200, self._analizza_selezionato)
        else:
            self.lbl_fase.configure(text="File salvato. Seleziona il prossimo e premi Analizza.")

    def _salta_file(self):
        if self.stato not in ("scelta_nr", "scelta_fondo"):
            return
        self._log("File saltato (nessun valore salvato).")
        indice = self.indice
        self._termina_file(salvato=False)
        self._ultimo_disegno = None
        self.fig.clear()
        self.canvas.draw_idle()
        self._seleziona_prossimo(indice + 1)

    def _chiudi_finestra(self):
        if self.stato == "lavoro" and not messagebox.askyesno(
                "Calcolo in corso", "C'è un calcolo in corso. Uscire comunque?"):
            return
        if self.analisi is not None:
            try:
                self.analisi.chiudi()
            except Exception:
                pass
        self.fig.clear()
        self.destroy()


def autotest():
    """Esegue l'intera analisi (lettura FITS, noise reduction, maschera, fondo,
    disegno e salvataggio PNG/CSV) su un'immagine sintetica, senza finestra.
    Serve a verificare che l'eseguibile contenga tutti i moduli necessari.
    Uso:  AnalisiFITS --autotest"""
    import tempfile
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    print("AUTOTEST Analisi FITS")
    print(f"Python {sys.version.split()[0]} | numpy {np.__version__} | "
          f"astropy {__import__('astropy').__version__} | "
          f"photutils {__import__('photutils').__version__} | "
          f"matplotlib {matplotlib.__version__}")
    cartella = Path(tempfile.mkdtemp(prefix="autotest_fits_"))
    try:
        rng = np.random.default_rng(0)
        yy, xx = np.mgrid[:700, :600]
        img = (rng.normal(0, 1, yy.shape) + 5 + 0.002 * xx
               + 40 * np.exp(-((yy - 350) ** 2 + (xx - 300) ** 2) / (2 * 60 ** 2))).astype(np.float32)
        img[:20] = np.nan
        percorso = cartella / "prova.fits"
        hdu = fits.ImageHDU(img, name="SCI")
        hdu.header["FILTER"], hdu.header["BUNIT"] = "TEST", "MJy/sr"
        fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(percorso)
        print("[ok] scrittura FITS")

        global TILE
        tile_originale = TILE
        for modalita in ("ram", "disco"):
            TILE = 256 if modalita == "disco" else tile_originale   # forza i tasselli
            a = AnalisiFile(percorso, None, modalita, 4.0, cartella, 400)
            pn = a.fase_noise_reduction([1.0, 2.0], lambda m: None)
            pf, mk = a.fase_fondo(1.0, pn[1]["snr"], [64, 128], lambda m: None)
            a.chiudi()
            print(f"[ok] analisi in modalità {modalita}: SNR {[round(float(p['snr']), 3) for p in pn]}, "
                  f"residuo {[round(p['residuo'][0], 3) for p in pf]}, maschera {mk.mean():.2f}")
        TILE = tile_originale

        fig = Figure(layout="constrained")
        FigureCanvasAgg(fig)
        assi = fig.subplots(1, len(pf), squeeze=False)
        for ax, p in zip(assi.flat, pf):
            v = p["vista"]
            vmin, vmax = ZScaleInterval().get_limits(v[np.isfinite(v)])
            ax.imshow(v, norm=ImageNormalize(vmin=vmin, vmax=vmax, stretch=AsinhStretch(a=ASINH_A)))
            ax.set_title(p["titolo"])
        fig.savefig(cartella / "prova.png", dpi=60)
        print("[ok] disegno e salvataggio PNG")

        scrivi_csv(cartella / "prova.csv", {"prova.fits": {"file": "prova.fits", "box_fondo_px": "64"}})
        assert leggi_csv(cartella / "prova.csv")["prova.fits"]["box_fondo_px"] == "64"
        print("[ok] tabella CSV")

        radice = tk.Tk()
        radice.withdraw()
        tkfont.nametofont("TkDefaultFont")
        radice.destroy()
        print("[ok] finestra Tk")
        print("AUTOTEST SUPERATO")
        return 0
    except Exception:
        traceback.print_exc()
        print("AUTOTEST FALLITO")
        return 1
    finally:
        shutil.rmtree(cartella, ignore_errors=True)


def _uscita_testo(nome):
    """Negli eseguibili senza console (Windows) stdout può non esistere:
    in quel caso si scrive in <nome>_risultato.txt nella cartella corrente."""
    if sys.stdout is None or not hasattr(sys.stdout, "write"):
        sys.stdout = sys.stderr = open(Path.cwd() / f"{nome}_risultato.txt", "w", encoding="utf-8", buffering=1)


def main():
    if "--autotest" in sys.argv:
        _uscita_testo("autotest")
        sys.exit(autotest())
    # Eseguibile senza console (PyInstaller --windowed): stdout/stderr non esistono.
    # Si mandano su un file di log nella home, utile se qualcosa va storto.
    if getattr(sys, "frozen", False) or sys.stderr is None:
        try:
            log = open(Path.home() / ".analisi_fits.log", "a", encoding="utf-8", buffering=1)
            log.write(f"\n--- avvio {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
            sys.stdout = sys.stderr = log
        except OSError:
            sys.stdout = sys.stderr = open(os.devnull, "w")
    if sys.platform == "win32":
        try:                                   # finestra nitida su schermi ad alta risoluzione
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    app = App()
    if sys.platform == "win32":
        try:                                   # icona nella barra delle applicazioni
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AnalisiFITS")
        except Exception:
            pass
    app.mainloop()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()           # necessario negli eseguibili PyInstaller
    main()
