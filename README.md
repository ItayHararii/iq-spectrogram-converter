# IQ Data Spectrogram Generator

Generates spectrogram heatmaps from IQ data stored in stereo `.wav` files (channel 1 = I, channel 2 = Q).

The script computes an STFT using a Blackman-Harris window, converts to power (dB), applies frequency-axis smoothing, and saves each result as a `.png` image.

## Requirements

```
pip install -r requirements.txt
```

Dependencies: `numpy`, `matplotlib`, `seaborn`, `scipy`, `soundfile`

## Usage

```bash
python iq_data.py --folder <path_to_wav_files> [--output <output_dir>] [--rbw <hz>]
python iq_data.py --input <path_to_wav_file> [--output <output_dir>] [--rbw <hz>]
```

### Arguments

| Argument   | Required           | Default            | Description                          |
|------------|--------------------|--------------------|--------------------------------------|
| `--folder` | Yes (or `--input`) | —                  | Folder containing `.wav` files       |
| `--input`  | Yes (or `--folder`)| —                  | Single `.wav` file to process        |
| `--output` | No                 | `<folder>_output` or `.` | Output folder for generated images |
| `--rbw`    | No                 | `15000`            | Resolution bandwidth in Hz           |

### Examples

```bash
# Process a single file, save PNG to current directory
python iq_data.py --input ./file_131219_0001.wav

# Process all .wav files, save PNGs to ./files_output/
python iq_data.py --folder ./files

# Custom output directory and RBW
python iq_data.py --folder ./files --output ./results --rbw 20000
```
