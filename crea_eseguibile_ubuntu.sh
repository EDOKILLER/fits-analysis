#!/usr/bin/env bash
# =============================================================================
# Crea l'eseguibile "AnalisiFITS" per Ubuntu (da fare UNA sola volta).
# L'eseguibile finale funziona senza Python, senza JupyterLab e senza pacchetti.
#
# Uso: metti questo file nella stessa cartella di analisi_fits.py, poi nel terminale:
#     cd <cartella con i due file>
#     bash crea_eseguibile_ubuntu.sh
# =============================================================================
set -e
cd "$(dirname "$(readlink -f "$0")")"

if [ ! -f analisi_fits.py ]; then
    echo "ERRORE: analisi_fits.py non è in questa cartella ($(pwd))."
    exit 1
fi

echo "=== 1/4  Strumenti di sistema (chiede la password) ==="
sudo apt update
sudo apt install -y python3-venv python3-tk

echo "=== 2/4  Ambiente Python temporaneo per la compilazione ==="
# l'ambiente viene riusato se esiste già (ricompilazioni molto più rapide)
[ -d .venv-build ] || python3 -m venv .venv-build
source .venv-build/bin/activate
python -m pip install --upgrade pip
python -m pip install numpy scipy matplotlib astropy photutils psutil pyinstaller

echo "=== 3/4  Creazione dell'eseguibile (qualche minuto) ==="
pyinstaller --noconfirm --clean --onefile --windowed \
    --name AnalisiFITS \
    --collect-all astropy \
    --collect-all photutils \
    --collect-all erfa \
    --collect-submodules scipy \
    --collect-binaries scipy \
    --collect-submodules matplotlib \
    --collect-data astropy_iers_data \
    --copy-metadata scipy \
    --copy-metadata numpy \
    --copy-metadata astropy \
    --copy-metadata photutils \
    --copy-metadata matplotlib \
    --copy-metadata pyerfa \
    --copy-metadata astropy-iers-data \
    --copy-metadata packaging \
    --copy-metadata pyyaml \
    --copy-metadata psutil \
    --copy-metadata pillow \
    --hidden-import matplotlib.backends.backend_tkagg \
    --hidden-import PIL._tkinter_finder \
    analisi_fits.py
deactivate

echo "=== Verifica dell'eseguibile (autotest) ==="
if ! ./dist/AnalisiFITS --autotest > autotest.txt 2>&1; then
    cat autotest.txt
    echo
    echo "L'autotest è FALLITO: i dettagli sono in $(pwd)/autotest.txt"
    echo "L'eseguibile non è stato installato. Invia autotest.txt per la correzione."
    rm -rf build AnalisiFITS.spec
    exit 1
fi
cat autotest.txt

echo "=== 4/4  Installazione nella home e icona nel menu ==="
DEST="$HOME/.local/bin"
mkdir -p "$DEST" "$HOME/.local/share/applications"
cp dist/AnalisiFITS "$DEST/AnalisiFITS"
chmod +x "$DEST/AnalisiFITS"

cat > "$HOME/.local/share/applications/analisi-fits.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Analisi FITS
Comment=Noise reduction e sottrazione del fondo per file FITS
Exec=$DEST/AnalisiFITS
Icon=applications-science
Terminal=false
Categories=Science;Astronomy;Education;
EOF
chmod +x "$HOME/.local/share/applications/analisi-fits.desktop"

# Copia anche sulla Scrivania (cartella Desktop o Scrivania, a seconda della lingua)
SCRIVANIA="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Scrivania")"
if [ -d "$SCRIVANIA" ]; then
    cp "$HOME/.local/share/applications/analisi-fits.desktop" "$SCRIVANIA/"
    chmod +x "$SCRIVANIA/analisi-fits.desktop"
    gio set "$SCRIVANIA/analisi-fits.desktop" metadata::trusted true 2>/dev/null || true
fi

# Pulizia dei file della compilazione (l'eseguibile resta in dist/ e in ~/.local/bin)
rm -rf .venv-build build AnalisiFITS.spec

echo
echo "FATTO."
echo "  Eseguibile:  $DEST/AnalisiFITS   (copia anche in $(pwd)/dist/)"
echo "  Avvio:       cerca 'Analisi FITS' nel menu delle applicazioni,"
echo "               oppure doppio clic sull'icona sulla Scrivania"
echo "               (la prima volta: tasto destro -> 'Consenti avvio')."
