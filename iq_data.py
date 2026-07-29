import argparse
import os
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from scipy.signal import stft
import soundfile as sf


def process_file(path, output_dir, rbw):
    x, fs = sf.read(path)

    I, Q = (x[:, 0], x[:, 1])
    iq = I + 1j * Q

    num_samples = len(iq)
    duration_s = num_samples / fs
    bandwidth_hz = fs  # Nyquist bandwidth for complex IQ
    file_size_kb = os.path.getsize(path) / 1024

    print(f"--- {Path(path).name} ---")
    print(f"  Sample rate    : {fs:,} Hz")
    print(f"  Samples        : {num_samples:,}")
    print(f"  Duration       : {duration_s:.3f} s")
    print(f"  Bandwidth      : {bandwidth_hz / 1e6:.3f} MHz")
    print(f"  RBW            : {rbw / 1e3:.3f} kHz")
    print(f"  File size      : {file_size_kb:.1f} kB")

    RBW = rbw
    bin_width = RBW / 2
    n_fft = int(2 ** np.ceil(np.log2(fs / RBW * 2)))
    # boundary=None, padded=False: same framing as scipy.signal.spectrogram(..., mode="psd", scaling="spectrum")
    f, t, Zxx = stft(
        iq,
        fs=fs,
        nperseg=n_fft,
        noverlap=n_fft // 2,
        window="blackmanharris",
        scaling="spectrum",
        return_onesided=False,
        boundary=None,
        padded=False,
    )
    # # Power matches spectrogram PSD: |Zxx|**2 with scaling="spectrum"
    power = np.abs(Zxx) ** 2
    power_db = 10 * np.log10(power)
    power = 10 ** (power_db / 10)

    window_size = 5
    kernel = np.ones(window_size) / window_size
    power_smooth = np.apply_along_axis(
        lambda x: np.convolve(x, kernel, mode="same"),
        axis=0,
        arr=power,
    )
    power_db = 10 * np.log10(power_smooth + 1e-12)

    power_db = np.fft.fftshift(power_db)

    plt.figure(figsize=(10, 10))
    sns.heatmap(power_db.T, cmap="jet")
    out_name = Path(path).stem + ".png"
    plt.savefig(os.path.join(output_dir, out_name), dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved          : {out_name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IQ data spectrogram generator")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--folder", help="Folder containing .wav files")
    group.add_argument("--input", help="Single .wav file to process")
    parser.add_argument("--output", default=None, help="Output folder (default: <folder>_output or current directory)")
    parser.add_argument("--rbw", type=float, default=15000, help="Resolution bandwidth in Hz (default: 15000)")
    args = parser.parse_args()

    if args.input:
        input_path = Path(args.input)
        if not input_path.is_file():
            print(f"File not found: {args.input}")
            exit(1)
        output_dir = args.output or "."
        os.makedirs(output_dir, exist_ok=True)
        process_file(str(input_path), output_dir, args.rbw)
    else:
        folder = args.folder
        output_dir = args.output or (folder.rstrip("/\\") + "_output")
        os.makedirs(output_dir, exist_ok=True)

        wav_files = sorted(Path(folder).glob("*.wav"))
        if not wav_files:
            print(f"No .wav files found in {folder}")
            exit(1)

        print(f"Found {len(wav_files)} .wav file(s). Saving results to {output_dir}/")
        for wav_path in wav_files:
            process_file(str(wav_path), output_dir, args.rbw)