# @title **🌀CrownScaler (Verified Stable Build - Exact Microwave Curve, Post-Upscale RSMB & 120 FPS Engine)🌀**

import base64
import contextlib
import gc
import http.server
import importlib
import io
import json
import math
import os
import queue
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import zipfile
from google.colab import output
import IPython
import numpy as np

# ==============================================================================
# 1. DIRECTORY SETUP & STATE MANAGEMENT
# ==============================================================================
_I_D_X = "/content/.sys_in_cache_0x9A"
_O_D_X = "/content/.sys_out_vault_0x9B"
_M_D_X = "/dev/shm/.k_core_weights_tmp"
_RIFE_D_X = "/content/Practical-RIFE/RIFE_v4.26"
_RIFE_REPO_D_X = "/content/Practical-RIFE_repo"
_REC_FILE = "/content/.sys_recovery_state.json"
_BOOT_TIME_FILE = "/content/.sys_boot_timer.txt"
_THUMB_CACHE = {}

os.makedirs(_I_D_X, exist_ok=True)
os.makedirs(_O_D_X, exist_ok=True)
os.makedirs(_M_D_X, exist_ok=True)
os.makedirs(_RIFE_D_X, exist_ok=True)

try:
    has_gpu = (
        subprocess.run(
            ["nvidia-smi"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).returncode
        == 0
    )
except Exception:
    has_gpu = False

DRIVE_SAVED_FILES = set()

SYSTEM_STATE = {
    "setup_status": "installing",
    "is_resume": None,
    "status": "idle",
    "progress": 0,
    "frames_done": 0,
    "frames_total": 0,
    "text": "Waiting for input...",
    "current_file": "",
    "error_log": "",
    "saved_files": [],
    "input_files": [],
    "has_gpu": has_gpu,
    "is_4k_plus": False,
    "is_interpolating": False,
    "is_remapped": False,
    "file_index_str": "",
    "last_completed": "",
    "completed_files": [],
    "new_completed_files": [],
    "unsupported_files": {},
    "recovery_config": None,
    "boot_start_time": None,
    "last_heartbeat": None,
}
IS_RUNNING = False


# ==============================================================================
# 2. COLAB PROXY MEDIA STREAMING SERVER (FOR IN-APP PREVIEWS)
# ==============================================================================
class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory="/", **kwargs)

    def translate_path(self, path):
        clean_path = path.split("?")[0]
        resolved_path = super().translate_path(clean_path)
        safe_dirs = [os.path.abspath(_I_D_X), os.path.abspath(_O_D_X)]
        for safe_dir in safe_dirs:
            if os.path.commonpath([resolved_path, safe_dir]) == safe_dir:
                return resolved_path
        return "/dev/null"

    @staticmethod
    def _parse_byte_range(range_header, size):
        """Return an inclusive byte range, or ``None`` when it is invalid."""
        if size <= 0 or not range_header:
            return None

        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match:
            return None

        start_text, end_text = match.groups()
        if not start_text and not end_text:
            return None
        if not start_text:
            suffix_len = int(end_text)
            if suffix_len <= 0:
                return None
            return max(0, size - suffix_len), size - 1

        first_byte = int(start_text)
        if first_byte >= size:
            return None
        last_byte = int(end_text) if end_text else size - 1
        if last_byte < first_byte:
            return None
        return first_byte, min(last_byte, size - 1)

    def send_head(self):
        path = self.translate_path(self.path)
        f = None
        if os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(http.server.HTTPStatus.NOT_FOUND, "File not found")
            return None

        try:
            fs = os.fstat(f.fileno())
            size = fs.st_size
            filename = os.path.basename(path)
            mime_type = (
                "video/mp4"
                if path.lower().endswith((".mp4", ".mov", ".mkv", ".avi"))
                else self.guess_type(path)
            )

            if "Range" in self.headers:
                byte_range = self._parse_byte_range(self.headers["Range"], size)
                if byte_range is None:
                    f.close()
                    self.send_response(http.server.HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return None
                first_byte, last_byte = byte_range
                length = last_byte - first_byte + 1

                self.send_response(http.server.HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(length))
                self.send_header(
                    "Content-Range", f"bytes {first_byte}-{last_byte}/{size}"
                )
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                f.seek(first_byte)
                return f
            else:
                self.send_response(http.server.HTTPStatus.OK)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return f
        except Exception:
            f.close()
            raise

    def copyfile(self, source, outputfile):
        if "Range" in self.headers:
            byte_range = self._parse_byte_range(
                self.headers["Range"], os.fstat(source.fileno()).st_size
            )
            if byte_range is not None:
                first_byte, last_byte = byte_range
                length = last_byte - first_byte + 1
                chunk_size = 1024 * 64
                while length > 0:
                    data = source.read(min(length, chunk_size))
                    if not data:
                        break
                    outputfile.write(data)
                    length -= len(data)
                return
        super().copyfile(source, outputfile)

    def log_message(self, format, *args):
        pass


PORT = 8050


def start_server():
    try:
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("", PORT), RangeRequestHandler) as httpd:
            httpd.serve_forever()
    except Exception:
        pass


threading.Thread(target=start_server, daemon=True).start()


# ==============================================================================
# 3. UTILITIES & ROBUST ENVIRONMENT SETUP
# ==============================================================================
def sanitize_filename(name):
    base, ext = os.path.splitext(name)
    base = re.sub(r"[^\w\-.]", "_", base)
    base = re.sub(r"_+", "_", base).strip("_")
    if not base:
        base = "media"
    return f"{base}{ext}"


def get_unique_filepath(target_dir, filename):
    clean_name = sanitize_filename(filename)
    base, ext = os.path.splitext(clean_name)
    out_path = os.path.join(target_dir, f"{base}{ext}")
    counter = 1
    while os.path.exists(out_path):
        out_path = os.path.join(target_dir, f"{base}_{counter}{ext}")
        counter += 1
    return out_path


def _is_safe_path(p):
    if not p:
        return False
    try:
        rp = os.path.realpath(p)
    except Exception:
        return False
    return (
        rp.startswith(os.path.realpath(_I_D_X) + os.sep)
        or rp.startswith(os.path.realpath(_O_D_X) + os.sep)
        or rp.startswith(os.path.realpath(_M_D_X) + os.sep)
    )


def _invalidate_thumb_cache(filepath):
    try:
        keys_to_del = [k for k in _THUMB_CACHE.keys() if k[0] == filepath]
        for k in keys_to_del:
            del _THUMB_CACHE[k]
    except Exception:
        pass


def get_file_metadata(filepath):
    try:
        mtime = os.path.getmtime(filepath)
        size_bytes = os.path.getsize(filepath)
        cache_key = (filepath, mtime, size_bytes)
        if cache_key in _THUMB_CACHE:
            return _THUMB_CACHE[cache_key]

        import cv2

        size_mb = size_bytes / (1024 * 1024)
        size_str = f"{size_mb:.1f} MB"
        res_str, b64_thumb = "Unknown", ""
        is_video = filepath.lower().endswith((".mp4", ".avi", ".mov", ".mkv"))
        is_zip = filepath.lower().endswith(".zip")

        if is_zip:
            img = np.zeros((200, 200, 3), dtype=np.uint8)
            img[:] = (255, 255, 255)
            cv2.putText(
                img,
                "ZIP",
                (50, 120),
                cv2.FONT_HERSHEY_SIMPLEX,
                2,
                (0, 0, 0),
                3,
                cv2.LINE_AA,
            )
            _, buffer = cv2.imencode(".png", img)
            b64_thumb = "data:image/png;base64," + base64.b64encode(buffer).decode(
                "utf-8"
            )
            res_str = "Archive"
            result = (size_str, res_str, b64_thumb)
            _THUMB_CACHE[cache_key] = result
            return result

        if is_video:
            cap = cv2.VideoCapture(filepath)
            if cap.isOpened():
                w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(
                    cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
                )
                fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                res_str = f"{w}x{h} ({int(round(fps))}fps)"
                ret, frame = cap.read()
                if ret and frame is not None:
                    th_h = 240
                    th_w = int((w / h) * th_h) if h > 0 else 320
                    _, buffer = cv2.imencode(
                        ".jpg",
                        cv2.resize(frame, (th_w, th_h)),
                        [cv2.IMWRITE_JPEG_QUALITY, 60],
                    )
                    b64_thumb = "data:image/jpeg;base64," + base64.b64encode(
                        buffer
                    ).decode("utf-8")
                cap.release()
        else:
            frame = cv2.imread(filepath, cv2.IMREAD_COLOR)
            if frame is not None:
                h, w, _ = frame.shape
                res_str = f"{w}x{h}"
                th_h = 300
                th_w = int((w / h) * th_h) if h > 0 else 300
                _, buffer = cv2.imencode(
                    ".jpg",
                    cv2.resize(frame, (th_w, th_h)),
                    [cv2.IMWRITE_JPEG_QUALITY, 70],
                )
                b64_thumb = "data:image/jpeg;base64," + base64.b64encode(buffer).decode(
                    "utf-8"
                )

        result = (size_str, res_str, b64_thumb)
        _THUMB_CACHE[cache_key] = result
        return result
    except Exception:
        return "Unknown", "Unknown", ""


def update_file_states():
    if os.path.exists(_REC_FILE):
        try:
            with open(_REC_FILE, "r") as f:
                SYSTEM_STATE["recovery_config"] = json.load(f)
        except Exception:
            SYSTEM_STATE["recovery_config"] = None
    else:
        SYSTEM_STATE["recovery_config"] = None

    out_files = []
    if os.path.exists(_O_D_X):
        for f in reversed(
            sorted(
                os.listdir(_O_D_X),
                key=lambda x: os.path.getmtime(os.path.join(_O_D_X, x)),
            )
        ):
            if f.startswith("."):
                continue
            p = os.path.join(_O_D_X, f)
            try:
                if ".tagfix." in f:
                    if not IS_RUNNING:
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                        _invalidate_thumb_cache(p)
                    continue

                sz, res, th = get_file_metadata(p)
                is_corrupted = os.path.getsize(p) == 0

                if is_corrupted:
                    if not IS_RUNNING:
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                        _invalidate_thumb_cache(p)
                    continue

                matched_input_path = ""
                orig_match = re.sub(r"^CrownScaler_(Remapped_)?", "", f)
                base_match, ext_match = os.path.splitext(orig_match)
                clean_base = re.sub(r"_\d+$", "", base_match)

                potential_originals = [
                    f"{clean_base}{ext_match}",
                    f"{base_match}{ext_match}",
                    orig_match,
                ]
                for pot_name in potential_originals:
                    cand_p = os.path.join(_I_D_X, pot_name)
                    if os.path.exists(cand_p):
                        matched_input_path = cand_p
                        break

                out_files.append({
                    "name": f,
                    "path": p,
                    "original_path": matched_input_path,
                    "size": sz,
                    "res": res,
                    "thumb": th,
                    "saved": p in DRIVE_SAVED_FILES,
                })
            except Exception:
                pass
    SYSTEM_STATE["saved_files"] = out_files

    in_files = []
    if os.path.exists(_I_D_X):
        for f in reversed(
            sorted(
                os.listdir(_I_D_X),
                key=lambda x: os.path.getmtime(os.path.join(_I_D_X, x)),
            )
        ):
            if f.startswith("."):
                continue
            p = os.path.join(_I_D_X, f)
            try:
                sz, res, th = get_file_metadata(p)
                in_files.append(
                    {"name": f, "path": p, "size": sz, "res": res, "thumb": th}
                )
            except Exception:
                pass
    SYSTEM_STATE["input_files"] = in_files


def get_system_state(dummy):
    return IPython.display.JSON(SYSTEM_STATE)


output.register_callback("notebook.get_state", get_system_state)


def get_file_info_for_download(path_str):
    try:
        if not _is_safe_path(path_str) or not os.path.exists(path_str):
            return IPython.display.JSON({"success": False, "error": "Invalid file path"})
        size = os.path.getsize(path_str)
        filename = os.path.basename(path_str)
        mime = (
            "video/mp4"
            if path_str.lower().endswith((".mp4", ".mov", ".mkv", ".avi"))
            else "application/octet-stream"
        )
        return IPython.display.JSON({
            "success": True,
            "filename": filename,
            "size": size,
            "mime": mime,
        })
    except Exception as e:
        return IPython.display.JSON({"success": False, "error": str(e)})


output.register_callback("notebook.get_file_info", get_file_info_for_download)


def get_file_chunk_base64(path_str, offset, length):
    try:
        offset = int(offset)
        length = int(length)
        if not _is_safe_path(path_str) or not os.path.exists(path_str):
            return IPython.display.JSON({"success": False, "error": "Invalid file path"})
        with open(path_str, "rb") as f:
            f.seek(offset)
            chunk = f.read(length)
        b64_chunk = base64.b64encode(chunk).decode("utf-8")
        return IPython.display.JSON({
            "success": True,
            "data": b64_chunk,
            "bytes_read": len(chunk),
        })
    except Exception as e:
        return IPython.display.JSON({"success": False, "error": str(e)})


output.register_callback("notebook.get_file_chunk", get_file_chunk_base64)


# ==============================================================================
# PROVEN STABLE ENVIRONMENT SETUP & RIFE PRE-FETCH
# ==============================================================================
def ensure_rife_setup():
    train_log_path = os.path.join(_RIFE_D_X, "train_log")
    model_dir = os.path.join(_RIFE_D_X, "model")
    os.makedirs(train_log_path, exist_ok=True)
    os.makedirs(model_dir, exist_ok=True)

    if not os.path.exists(_RIFE_REPO_D_X):
        subprocess.run(
            ["git", "clone", "https://github.com/hzwer/Practical-RIFE.git", _RIFE_REPO_D_X],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if not os.path.exists(os.path.join(train_log_path, "flownet.pkl")):
        zip_path = os.path.join(_RIFE_D_X, "model.zip")
        urllib.request.urlretrieve(
            "https://huggingface.co/r3gm/RIFE/resolve/main/RIFEv4.26_0921.zip",
            zip_path,
        )
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(_RIFE_D_X)
        if os.path.exists(zip_path):
            os.remove(zip_path)

        for root, dirs, files_in_dir in os.walk(_RIFE_D_X):
            if os.path.abspath(root) == os.path.abspath(train_log_path):
                continue
            for item in files_in_dir:
                if item.endswith(".pkl"):
                    shutil.move(os.path.join(root, item), os.path.join(train_log_path, item))

    repo_model = os.path.join(_RIFE_REPO_D_X, "model")
    if os.path.exists(repo_model):
        for item in os.listdir(repo_model):
            src_f = os.path.join(repo_model, item)
            if os.path.isfile(src_f):
                shutil.copy(src_f, os.path.join(model_dir, item))
                shutil.copy(src_f, os.path.join(train_log_path, item))

    open(os.path.join(_RIFE_D_X, "__init__.py"), "a").close()
    open(os.path.join(model_dir, "__init__.py"), "a").close()
    open(os.path.join(train_log_path, "__init__.py"), "a").close()


def setup_environment():
    global SYSTEM_STATE
    try:
        import cv2, gdown, numpy, tensorrt, torch

        SYSTEM_STATE["is_resume"] = True
        SYSTEM_STATE["boot_start_time"] = time.time()
        time.sleep(0.5)
    except ImportError:
        SYSTEM_STATE["is_resume"] = False
        start_ts = time.time()
        SYSTEM_STATE["boot_start_time"] = start_ts
        try:
            with open(_BOOT_TIME_FILE, "w") as f:
                f.write(str(start_ts))
        except Exception:
            pass

        os.system("apt-get install -y ffmpeg git wget > /dev/null 2>&1")
        os.system(
            "uv pip install tensorrt==10.16.1.11 gdown opencv-python-headless >"
            " /dev/null 2>&1"
        )

    try:
        ensure_rife_setup()
    except Exception:
        pass

    if os.path.exists(_BOOT_TIME_FILE):
        try:
            os.remove(_BOOT_TIME_FILE)
        except Exception:
            pass

    try:
        update_file_states()
    except Exception:
        pass
    SYSTEM_STATE["setup_status"] = "complete"


threading.Thread(target=setup_environment, daemon=True).start()


def handle_upload_chunk(filename, b64_data, is_first, is_last=False):
    clean_name = sanitize_filename(os.path.basename(filename))
    if not clean_name or clean_name.startswith("."):
        return
    part_path = os.path.join(_I_D_X, f".{clean_name}.part")
    final_path = os.path.join(_I_D_X, clean_name)
    mode = "wb" if is_first else "ab"
    try:
        chunk = base64.b64decode(b64_data, validate=True)
        with open(part_path, mode) as f:
            f.write(chunk)
        if is_last and os.path.exists(part_path):
            if os.path.exists(final_path):
                final_path = get_unique_filepath(_I_D_X, clean_name)
            os.replace(part_path, final_path)
            _invalidate_thumb_cache(final_path)
    except Exception:
        traceback.print_exc()


output.register_callback("notebook.upload_chunk", handle_upload_chunk)


def import_url_media(url_str):
    url = url_str.strip()
    if not url:
        return IPython.display.JSON({"success": False, "error": "Empty URL provided."})

    try:
        import gdown
        import cv2

        temp_dest = os.path.join(_I_D_X, f"import_temp_{int(time.time())}")
        if "drive.google.com" in url:
            gdown.download(url=url, output=temp_dest, quiet=False, fuzzy=True)
        else:
            subprocess.run(["wget", "-q", "-O", temp_dest, url], check=False)

        if not os.path.exists(temp_dest) or os.path.getsize(temp_dest) < 100:
            if os.path.exists(temp_dest):
                os.remove(temp_dest)
            return IPython.display.JSON(
                {"success": False, "error": "Failed to download media file from URL."}
            )

        is_valid = False
        ext = ".mp4"

        cap = cv2.VideoCapture(temp_dest)
        if (
            cap.isOpened()
            and int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) > 0
            and int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) > 0
        ):
            is_valid = True
            ext = ".mp4"
            cap.release()
        else:
            cap.release()
            img = cv2.imread(temp_dest)
            if img is not None and img.shape[0] > 0 and img.shape[1] > 0:
                is_valid = True
                ext = ".png"

        if not is_valid:
            if os.path.exists(temp_dest):
                os.remove(temp_dest)
            return IPython.display.JSON(
                {
                    "success": False,
                    "error": "The link provided is not a valid video or image.",
                }
            )

        raw_base_name = os.path.basename(url.split("?")[0])
        if (
            not raw_base_name
            or len(raw_base_name) < 3
            or not re.search(
                r"\.(mp4|avi|mov|mkv|png|jpg|jpeg|webp)$", raw_base_name, re.I
            )
        ):
            final_name = f"imported_media_{int(time.time())}{ext}"
        else:
            final_name = sanitize_filename(raw_base_name)

        final_path = get_unique_filepath(_I_D_X, final_name)
        os.replace(temp_dest, final_path)
        _invalidate_thumb_cache(final_path)
        update_file_states()
        return IPython.display.JSON(
            {"success": True, "filename": os.path.basename(final_path)}
        )
    except Exception as e:
        return IPython.display.JSON({"success": False, "error": str(e)})


output.register_callback("notebook.import_url", import_url_media)


def handle_model_upload_chunk(filename, b64_data, is_first, is_last=False):
    ext = os.path.splitext(filename)[1].lower()
    if ext not in [".engine", ".onnx"]:
        ext = ".engine"

    dest_path = os.path.join(_M_D_X, f"custom_user_model{ext}")
    part_path = os.path.join(_M_D_X, f".custom_user_model{ext}.part")
    mode = "wb" if is_first else "ab"

    try:
        chunk = base64.b64decode(b64_data, validate=True)
        with open(part_path, mode) as f:
            f.write(chunk)
        if is_last and os.path.exists(part_path):
            os.replace(part_path, dest_path)

            other_ext = ".onnx" if ext == ".engine" else ".engine"
            other_path = os.path.join(_M_D_X, f"custom_user_model{other_ext}")
            if os.path.exists(other_path):
                try:
                    os.remove(other_path)
                except Exception:
                    pass

    except Exception:
        traceback.print_exc()


output.register_callback("notebook.upload_model_chunk", handle_model_upload_chunk)


def force_sync_files(dummy):
    try:
        update_file_states()
        return IPython.display.JSON({"success": True})
    except Exception as e:
        traceback.print_exc()
        return IPython.display.JSON({"success": False, "error": str(e)})


output.register_callback("notebook.force_sync", force_sync_files)


# ==============================================================================
# 4. PRE/POST PROCESSING GPU PIPELINE & REAL TIME MOTION BLUR
# ==============================================================================
def apply_pre_denoise_gpu(input_tensor, denoise_val=0):
    import torch
    import torch.nn.functional as F

    if denoise_val <= 0:
        return input_tensor

    d_strength = (float(denoise_val) / 100.0) * 0.75
    blurred = F.avg_pool2d(input_tensor, kernel_size=3, stride=1, padding=1)
    diff = torch.abs(input_tensor - blurred)
    weight = torch.exp(-diff * 15.0) * d_strength
    return (input_tensor * (1.0 - weight)) + (blurred * weight)


def apply_realtime_motion_blur_gpu(current_tensor, history_tensors, blur_val=0, shutter_mult=1.0):
    import torch

    if blur_val <= 0 or not history_tensors:
        return current_tensor

    strength = (float(blur_val) / 100.0) * 0.5 * shutter_mult
    strength = min(0.75, max(0.0, strength))

    num_hist = len(history_tensors)
    weights = [strength * (0.6 ** (num_hist - i)) for i in range(num_hist)]
    total_w = sum(weights)

    accum = current_tensor * (1.0 - total_w)
    for w_i, hist_t in zip(weights, history_tensors):
        accum += hist_t * w_i

    return accum.clamp(0.0, 1.0)


def apply_post_tuning_gpu(
    out_rgb_tensor,
    orig_rgb_tensor,
    recover_details=0,
    sharpen_val=0,
    dehalo_val=0,
):
    import torch
    import torch.nn.functional as F

    h_out, w_out = out_rgb_tensor.shape[2], out_rgb_tensor.shape[3]

    if recover_details > 0 and orig_rgb_tensor is not None:
        strength = float(recover_details) / 100.0
        orig_scaled = F.interpolate(
            orig_rgb_tensor, size=(h_out, w_out), mode="bilinear", align_corners=False
        )
        orig_low = F.avg_pool2d(orig_scaled, kernel_size=5, stride=1, padding=2)
        orig_high = orig_scaled - orig_low
        out_rgb_tensor = out_rgb_tensor + (orig_high * (strength * 0.85))

    if dehalo_val > 0:
        h_strength = (float(dehalo_val) / 100.0) * 0.55
        local_min = -F.max_pool2d(
            -out_rgb_tensor, kernel_size=3, stride=1, padding=1
        )
        local_max = F.max_pool2d(out_rgb_tensor, kernel_size=3, stride=1, padding=1)
        clamped = torch.clamp(out_rgb_tensor, min=local_min, max=local_max)
        out_rgb_tensor = (out_rgb_tensor * (1.0 - h_strength)) + (
            clamped * h_strength
        )

    if sharpen_val > 0:
        s_strength = (float(sharpen_val) / 100.0) * 1.25
        blurred = F.avg_pool2d(out_rgb_tensor, kernel_size=3, stride=1, padding=1)
        high_freq = out_rgb_tensor - blurred
        out_rgb_tensor = out_rgb_tensor + (high_freq * s_strength)

    return out_rgb_tensor.clamp_(0.0, 1.0)


# ==============================================================================
# 5. TILE-BASED SEAMLESS INFERENCE ENGINE (OOM-SAFE ACCUMULATOR)
# ==============================================================================
def process_frame_tiled(
    frame_bgr,
    context,
    engine,
    input_name,
    output_name,
    input_torch_dtype,
    out_torch_dtype,
    cuda_stream,
    scale_factor=4,
    tile_size=960,
    overlap=32,
    recover_details=0,
    sharpen_val=0,
    denoise_val=0,
    dehalo_val=0,
):
    import torch
    import torch.nn.functional as F

    h, w = frame_bgr.shape[:2]

    frame_gpu = torch.from_numpy(np.ascontiguousarray(frame_bgr)).to(
        "cuda", non_blocking=True
    )
    raw_rgb = (
        frame_gpu.flip(-1)
        .permute(2, 0, 1)
        .unsqueeze(0)
        .float()
        .div_(255.0)
        .contiguous()
    )

    denoised_rgb = apply_pre_denoise_gpu(raw_rgb, denoise_val=denoise_val)
    frame_rgb = denoised_rgb.to(input_torch_dtype).contiguous()

    if 256 <= h <= 1920 and 256 <= w <= 1920 and h % 8 == 0 and w % 8 == 0:
        if not context.set_input_shape(input_name, (1, 3, h, w)):
            raise RuntimeError(f"Engine rejected input shape: (1, 3, {h}, {w})")

        out_shape = tuple(context.get_tensor_shape(output_name))
        if any(d == -1 for d in out_shape):
            raise RuntimeError(f"Unresolved output tensor shape: {out_shape}")

        out_tensor = torch.empty(
            out_shape, dtype=out_torch_dtype, device="cuda"
        ).contiguous()

        context.set_tensor_address(input_name, frame_rgb.data_ptr())
        context.set_tensor_address(output_name, out_tensor.data_ptr())

        context.execute_async_v3(stream_handle=cuda_stream.cuda_stream)
        cuda_stream.synchronize()

        out_rgb_tensor = out_tensor.float().clamp_(0, 1)

        expected_h = h * scale_factor
        expected_w = w * scale_factor
        actual_h, actual_w = out_rgb_tensor.shape[2], out_rgb_tensor.shape[3]

        if actual_h != expected_h or actual_w != expected_w:
            crop_h = min(actual_h, expected_h)
            crop_w = min(actual_w, expected_w)
            out_rgb_tensor = out_rgb_tensor[:, :, :crop_h, :crop_w]
            if actual_h < expected_h or actual_w < expected_w:
                pad_bottom = expected_h - crop_h
                pad_right = expected_w - crop_w
                out_rgb_tensor = F.pad(
                    out_rgb_tensor, (0, pad_right, 0, pad_bottom), mode="replicate"
                )

        out_rgb_tensor = apply_post_tuning_gpu(
            out_rgb_tensor,
            raw_rgb,
            recover_details=recover_details,
            sharpen_val=sharpen_val,
            dehalo_val=dehalo_val,
        )

        return out_rgb_tensor

    out_h, out_w = h * scale_factor, w * scale_factor
    est_canvas_vram = (out_h * out_w * 4 * 4) / (1024 * 1024 * 1024)
    use_cpu_canvas = est_canvas_vram > 2.0
    canvas_device = "cpu" if use_cpu_canvas else "cuda"

    output_canvas = torch.zeros(
        (out_h, out_w, 3), dtype=torch.float32, device=canvas_device
    )
    weight_mask = torch.zeros(
        (out_h, out_w, 1), dtype=torch.float32, device=canvas_device
    )

    stride = tile_size - overlap

    for y in range(0, h, stride):
        for x in range(0, w, stride):
            y_end = min(y + tile_size, h)
            x_end = min(x + tile_size, w)
            y_start = max(0, y_end - tile_size)
            x_start = max(0, x_end - tile_size)

            tile_tensor = frame_rgb[:, :, y_start:y_end, x_start:x_end].contiguous()
            th, tw = tile_tensor.shape[2], tile_tensor.shape[3]

            target_th = max(256, ((th + 7) // 8) * 8)
            target_tw = max(256, ((tw + 7) // 8) * 8)

            pad_bottom = target_th - th
            pad_right = target_tw - tw

            if pad_bottom > 0 or pad_right > 0:
                tile_tensor = F.pad(
                    tile_tensor, (0, pad_right, 0, pad_bottom), mode="replicate"
                ).contiguous()

            if not context.set_input_shape(
                input_name, (1, 3, target_th, target_tw)
            ):
                raise RuntimeError(
                    f"Engine rejected tile shape: (1, 3, {target_th}, {target_tw})"
                )

            out_shape = tuple(context.get_tensor_shape(output_name))
            if any(d == -1 for d in out_shape):
                raise RuntimeError(f"Unresolved tile output shape: {out_shape}")

            out_tile_buf = torch.empty(
                out_shape, dtype=out_torch_dtype, device="cuda"
            ).contiguous()
            context.set_tensor_address(input_name, tile_tensor.data_ptr())
            context.set_tensor_address(output_name, out_tile_buf.data_ptr())

            context.execute_async_v3(stream_handle=cuda_stream.cuda_stream)
            cuda_stream.synchronize()

            valid_out_h = th * scale_factor
            valid_out_w = tw * scale_factor
            tile_out = (
                out_tile_buf.float()
                .clamp_(0, 1)
                .squeeze(0)
                .permute(1, 2, 0)
            )
            tile_out = tile_out[:valid_out_h, :valid_out_w, :]

            mask = torch.ones(
                (valid_out_h, valid_out_w, 1), dtype=torch.float32, device="cuda"
            )
            feather = overlap * scale_factor

            if feather > 0 and (valid_out_h > feather or valid_out_w > feather):
                feather_len_y = min(feather, valid_out_h)
                feather_len_x = min(feather, valid_out_w)
                feather_vec_y = torch.linspace(0, 1, feather_len_y, device="cuda")
                feather_vec_x = torch.linspace(0, 1, feather_len_x, device="cuda")

                if y_start > 0:
                    mask[:feather_len_y, :, 0] *= feather_vec_y.unsqueeze(1)
                if y_end < h:
                    mask[-feather_len_y:, :, 0] *= feather_vec_y.flip(0).unsqueeze(1)
                if x_start > 0:
                    mask[:, :feather_len_x, 0] *= feather_vec_x.unsqueeze(0)
                if x_end < w:
                    mask[:, -feather_len_x:, 0] *= feather_vec_x.flip(0).unsqueeze(0)

            oy_start = y_start * scale_factor
            ox_start = x_start * scale_factor
            oy_end = oy_start + valid_out_h
            ox_end = ox_start + valid_out_w

            if use_cpu_canvas:
                tile_out_c = tile_out.to("cpu", non_blocking=True)
                mask_c = mask.to("cpu", non_blocking=True)
                output_canvas[oy_start:oy_end, ox_start:ox_end] += tile_out_c * mask_c
                weight_mask[oy_start:oy_end, ox_start:ox_end] += mask_c
            else:
                output_canvas[oy_start:oy_end, ox_start:ox_end] += tile_out * mask
                weight_mask[oy_start:oy_end, ox_start:ox_end] += mask

    output_canvas = output_canvas / torch.clamp(weight_mask, min=1e-6)
    if use_cpu_canvas:
        out_rgb_tensor = output_canvas.clamp(0, 1).to("cuda").permute(2, 0, 1).unsqueeze(0)
    else:
        out_rgb_tensor = output_canvas.clamp(0, 1).permute(2, 0, 1).unsqueeze(0)

    out_rgb_tensor = apply_post_tuning_gpu(
        out_rgb_tensor,
        raw_rgb,
        recover_details=recover_details,
        sharpen_val=sharpen_val,
        dehalo_val=dehalo_val,
    )

    return out_rgb_tensor


# ==============================================================================
# 6. RIFE 120 FPS & EXACT TIME REMAP CUBIC BEZIER LUT + DEAD FRAME DECIMATION
# ==============================================================================
def load_rife_interpolator():
    ensure_rife_setup()

    paths_to_register = [
        _RIFE_REPO_D_X,
        _RIFE_D_X,
        os.path.join(_RIFE_D_X, "train_log"),
        os.path.join(_RIFE_D_X, "model"),
        os.path.join(_RIFE_REPO_D_X, "model"),
    ]
    for p in paths_to_register:
        if os.path.exists(p) and p not in sys.path:
            sys.path.insert(0, p)

    try:
        try:
            from train_log.RIFE_HDv3 import Model
        except ImportError:
            try:
                from model.RIFE_HDv3 import Model
            except ImportError:
                from RIFE_HDv3 import Model

        train_log = os.path.join(_RIFE_D_X, "train_log")
        model = Model()
        if not hasattr(model, "version"):
            model.version = 0
        model.load_model(train_log, -1)
        model.eval()
        model.device()
        return model
    except Exception as e:
        print(f"⚠️ RIFE model initialization error: {e}")
        traceback.print_exc()
        return None


def interpolate_pair_rife(rife_model, f0_bgr, f1_bgr, multi, device="cuda"):
    import torch
    import torch.nn.functional as F

    if rife_model is None or multi <= 1:
        return []

    h, w = f0_bgr.shape[:2]
    tmp = 64
    ph = ((h - 1) // tmp + 1) * tmp
    pw = ((w - 1) // tmp + 1) * tmp
    padding = (0, pw - w, 0, ph - h)

    t0 = torch.from_numpy(np.ascontiguousarray(f0_bgr[:, :, ::-1])).permute(2, 0, 1).unsqueeze(0).float().to(device) / 255.0
    t1 = torch.from_numpy(np.ascontiguousarray(f1_bgr[:, :, ::-1])).permute(2, 0, 1).unsqueeze(0).float().to(device) / 255.0

    t0 = F.pad(t0, padding)
    t1 = F.pad(t1, padding)

    try:
        rife_version = float(getattr(rife_model, "version", 0))
    except (TypeError, ValueError):
        rife_version = 0.0

    inter_frames = []
    with torch.no_grad():
        for i in range(1, multi):
            step = i / float(multi)
            if rife_version >= 3.9:
                mid = rife_model.inference(t0, t1, step, 1.0)
            else:
                # Older RIFE models do not accept a timestep.  Interpolate the
                # requested instant from the nearest recursively-generated pair
                # instead of emitting the same midpoint for every output frame.
                left, right = t0, t1
                low, high = 0.0, 1.0
                while (high - low) > (1.0 / multi):
                    midpoint = rife_model.inference(left, right, 1.0)
                    split = (low + high) / 2.0
                    if step < split:
                        right, high = midpoint, split
                    elif step > split:
                        left, low = midpoint, split
                    else:
                        left = right = midpoint
                        break
                mid = rife_model.inference(left, right, 1.0)

            mid_img = (
                (mid[0].clamp(0, 1) * 255.0)
                .byte()
                .permute(1, 2, 0)
                .cpu()
                .numpy()[:h, :w, ::-1]
            )
            inter_frames.append(mid_img)

    return inter_frames


def remove_dead_frames_list(frame_list, threshold=3.0):
    import cv2

    if len(frame_list) <= 1 or threshold <= 0:
        return frame_list

    clean_frames = [frame_list[0]]
    prev_gray = cv2.cvtColor(frame_list[0], cv2.COLOR_BGR2GRAY)
    diff_thresh = (float(threshold) / 100.0) * 255.0

    for idx in range(1, len(frame_list)):
        curr_frame = frame_list[idx]
        curr_gray = cv2.cvtColor(curr_frame, cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(prev_gray, curr_gray)
        mean_diff = float(np.mean(diff))

        if mean_diff >= diff_thresh:
            clean_frames.append(curr_frame)
            prev_gray = curr_gray

    return clean_frames


def _get_cubic_bezier_pt(t, p0, p1, p2, p3):
    cx = 3.0 * (p1[0] - p0[0])
    bx = 3.0 * (p2[0] - p1[0]) - cx
    ax = p3[0] - p0[0] - cx - bx
    cy = 3.0 * (p1[1] - p0[1])
    by = 3.0 * (p2[1] - p1[1]) - cy
    ay = p3[1] - p0[1] - cy - by
    return (
        (ax * (t**3)) + (bx * (t**2)) + (cx * t) + p0[0],
        (ay * (t**3)) + (by * (t**2)) + (cy * t) + p0[1],
    )


def build_microwave_lut():
    """Return a smooth forward-then-reverse time-remap curve.

    A cosine-eased arc has zero velocity at the beginning, turnaround, and
    end.  This avoids the abrupt changes in sampling direction caused by the
    previous joined Bezier segments.
    """
    lut_x = np.linspace(0.0, 1.0, 1001, dtype=np.float32)
    lut_y = 0.5 * (1.0 - np.cos(2.0 * np.pi * lut_x))
    return lut_x, lut_y.astype(np.float32)


def build_atempo_filter(speed_factor):
    """Build a valid FFmpeg atempo chain for a positive playback speed."""
    speed = float(speed_factor)
    if speed <= 0:
        raise ValueError("Speed factor must be greater than zero.")

    filters = []
    while speed < 0.5:
        filters.append("atempo=0.5")
        speed /= 0.5
    while speed > 2.0:
        filters.append("atempo=2.0")
        speed /= 2.0
    filters.append(f"atempo={speed:.8g}")
    return ",".join(filters)


# ==============================================================================
# 7. COMPUTE CORE PIPELINE
# ==============================================================================
def compile_onnx_to_trt(onnx_path, engine_path, trt_module):
    TRT_LOGGER = trt_module.Logger(trt_module.Logger.WARNING)
    builder = trt_module.Builder(TRT_LOGGER)

    flag = 1 << int(trt_module.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
    network = builder.create_network(flag)
    parser = trt_module.OnnxParser(network, TRT_LOGGER)

    with open(onnx_path, "rb") as model_file:
        if not parser.parse(model_file.read()):
            errs = [parser.get_error(i).desc() for i in range(parser.num_errors)]
            raise RuntimeError(f"ONNX Parsing failed: {errs}")

    b_config = builder.create_builder_config()
    if builder.platform_has_fast_fp16:
        b_config.set_flag(trt_module.BuilderFlag.FP16)

    profile = builder.create_optimization_profile()
    has_dynamic = False
    for i in range(network.num_inputs):
        in_tensor = network.get_input(i)
        if any(d == -1 for d in in_tensor.shape):
            has_dynamic = True
            profile.set_shape(
                in_tensor.name, (1, 3, 64, 64), (1, 3, 512, 512), (1, 3, 1920, 1920)
            )

    if has_dynamic:
        b_config.add_optimization_profile(profile)

    engine_bytes = builder.build_serialized_network(network, b_config)
    if engine_bytes is None:
        raise RuntimeError("Failed to build TensorRT engine from ONNX graph.")

    with open(engine_path, "wb") as f:
        f.write(engine_bytes)


def run_pipeline(config):
    global IS_RUNNING, SYSTEM_STATE
    if not SYSTEM_STATE["has_gpu"]:
        SYSTEM_STATE["status"] = "error"
        SYSTEM_STATE["error_log"] = (
            "Limit reached for today, change your google account to bypass it or"
            " wait hours so you can use it again."
        )
        IS_RUNNING = False
        if os.path.exists(_REC_FILE):
            try:
                os.remove(_REC_FILE)
            except Exception:
                pass
        return

    import cv2, gdown, tensorrt as trt, torch

    def gpu_cleanup(engine=None, context=None, rife_model=None):
        try:
            if context is not None:
                del context
            if engine is not None:
                del engine
            if rife_model is not None:
                del rife_model
            torch.cuda.empty_cache()
            gc.collect()
        except Exception:
            pass

    engine = None
    context = None
    rife_model = None

    try:
        files_to_process = config.get("files", [])
        if not files_to_process:
            raise ValueError("No files provided.")

        SYSTEM_STATE["status"] = "running"
        SYSTEM_STATE["text"] = "Verifying Neural Cores..."
        SYSTEM_STATE["unsupported_files"] = {}
        if not config.get("is_recovery"):
            SYSTEM_STATE["completed_files"] = []
            SYSTEM_STATE["new_completed_files"] = []
        else:
            SYSTEM_STATE["new_completed_files"] = []
            pre_recovery_completed = set(SYSTEM_STATE["completed_files"])
        if not config.get("is_recovery"):
            pre_recovery_completed = set()

        fps_120_requested = bool(config.get("fps120", False))
        remove_dead_requested = bool(config.get("removeDeadFrames", False))
        dead_threshold = float(config.get("deadThreshold", 3.0))
        speed_factor = float(config.get("speedFactor", 1.0))
        if speed_factor <= 0:
            raise ValueError("Speed factor must be greater than zero.")

        reverse_remap_requested = bool(config.get("reverseRemap", False))
        rsmb_blur_val = int(config.get("rsmbVal", 0))
        shutter_mode = str(config.get("shutterMode", "180"))
        shutter_multiplier = 1.0
        if shutter_mode == "270":
            shutter_multiplier = 1.5
        elif shutter_mode == "360":
            shutter_multiplier = 2.0

        if fps_120_requested:
            SYSTEM_STATE["text"] = "Initializing RIFE 120 FPS Neural Core..."
            rife_model = load_rife_interpolator()
            if rife_model is None:
                raise RuntimeError(
                    "120 FPS interpolation was requested, but the RIFE model could not be initialized."
                )

        lut_x, lut_y = build_microwave_lut()

        model_registry = {
            "Anime Ultra": {
                "file": "Crown_Ultra_High.engine",
                "url": "https://huggingface.co/Braveyukio/Anime_Pro/resolve/main/Anime%20(Balanced%2BVery%20Fast).engine?download=true",
            },
            "Anime-Pro": {
                "file": "Crown_Strong_(Compact).engine",
                "url": "https://huggingface.co/Braveyukio/Anime_Pro/resolve/main/Anime%20(Quality%20%2B%20Fast).engine?download=true",
            },
            "Real World": {
                "file": "Crown_Real_World_Base.engine",
                "url": "https://huggingface.co/Braveyukio/Anime_Pro/resolve/main/Real%20World%20Clips%20(Fast).engine?download=true",
            },
        }

        selected_model = config.get("model", "Anime-Pro")
        engine_path = None

        if selected_model == "Custom":
            custom_url = config.get("customModelUrl", "").strip()
            engine_path = os.path.join(_M_D_X, "custom_user_model.engine")
            onnx_path = os.path.join(_M_D_X, "custom_user_model.onnx")

            if custom_url:
                is_onnx_url = custom_url.lower().endswith(".onnx") or "onnx" in custom_url.lower()
                target_path = onnx_path if is_onnx_url else engine_path
                other_path = engine_path if is_onnx_url else onnx_path

                if not os.path.exists(target_path):
                    SYSTEM_STATE["text"] = "Downloading Custom Model..."
                    if "drive.google.com" in custom_url:
                        gdown.download(url=custom_url, output=target_path, quiet=False, fuzzy=True)
                    else:
                        subprocess.run(["wget", "-q", "-O", target_path, custom_url], check=False)

                    if os.path.exists(other_path):
                        try:
                            os.remove(other_path)
                        except Exception:
                            pass

            if os.path.exists(onnx_path):
                need_compile = not os.path.exists(engine_path) or (
                    os.path.getmtime(onnx_path) > os.path.getmtime(engine_path)
                )
                if need_compile:
                    SYSTEM_STATE["text"] = "Compiling ONNX to TensorRT (Takes 1-3 mins)..."
                    compile_onnx_to_trt(onnx_path, engine_path, trt)

            if not os.path.exists(engine_path) or os.path.getsize(engine_path) < 1024 * 1024:
                raise ValueError("Custom model file is missing or invalid. Please upload a valid .engine or .onnx model.")
        else:
            model_meta = model_registry.get(selected_model, model_registry["Anime-Pro"])
            engine_path = os.path.join(_M_D_X, model_meta["file"])

            if not os.path.exists(engine_path) or os.path.getsize(engine_path) < 1024 * 1024:
                SYSTEM_STATE["text"] = "Downloading Model from Hugging Face..."
                subprocess.run(["wget", "-q", "-O", engine_path, model_meta["url"]], check=False)

            if not os.path.exists(engine_path) or os.path.getsize(engine_path) < 100 * 1024:
                try:
                    if os.path.exists(engine_path):
                        os.remove(engine_path)
                except Exception:
                    pass
                raise RuntimeError("Failed to download model weights from Hugging Face. Check network connection.")

        SYSTEM_STATE["text"] = "Allocating TensorRT Context..."
        TRT_LOGGER = trt.Logger(trt.Logger.WARNING)
        try:
            with open(engine_path, "rb") as f, trt.Runtime(TRT_LOGGER) as runtime:
                engine = runtime.deserialize_cuda_engine(f.read())
        except Exception:
            raise RuntimeError(
                "Incompatible TensorRT engine file detected! Precompiled engines must match this exact GPU architecture. "
                "Please provide the source .onnx model instead so CrownScaler can compile it automatically."
            )

        if engine is None:
            raise RuntimeError("Failed to deserialize CUDA Engine. Ensure the model matches this GPU architecture.")

        context = engine.create_execution_context()

        input_name = None
        output_name = None
        for i in range(engine.num_io_tensors):
            name = engine.get_tensor_name(i)
            if engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
                input_name = name
            else:
                output_name = name

        in_dtype = engine.get_tensor_dtype(input_name)
        input_torch_dtype = (
            torch.float32 if in_dtype == trt.DataType.FLOAT else torch.float16
        )

        out_dtype = engine.get_tensor_dtype(output_name)
        out_torch_dtype = (
            torch.float32 if out_dtype == trt.DataType.FLOAT else torch.float16
        )

        probe_h, probe_w = 256, 256
        if not context.set_input_shape(input_name, (1, 3, probe_h, probe_w)):
            raise RuntimeError(f"Engine rejected baseline shape probe (1, 3, {probe_h}, {probe_w}).")

        out_probe_shape = tuple(context.get_tensor_shape(output_name))
        if any(d == -1 for d in out_probe_shape):
            raise RuntimeError(
                "Cannot determine model output shape for scale detection."
            )
        probe_out_h, probe_out_w = out_probe_shape[2], out_probe_shape[3]
        scale_factor = max(1, round(probe_out_h / probe_h))

        recover_details_val = int(config.get("recoverDetails", 0))
        sharpen_setting = int(config.get("sharpenVal", 0))
        denoise_setting = int(config.get("denoiseVal", 0))
        dehalo_setting = int(config.get("dehaloVal", 0))

        total_files = len(files_to_process)
        for idx, filename in enumerate(files_to_process):
            if not IS_RUNNING:
                break

            clean_file_name = sanitize_filename(os.path.basename(str(filename)))
            base_name, ext = os.path.splitext(clean_file_name)
            prefix = "CrownScaler_Remapped_" if reverse_remap_requested else "CrownScaler_"
            expected_out_name = f"{prefix}{base_name}{ext}"

            if config.get("is_recovery"):
                already_done = False
                for completed_f in SYSTEM_STATE.get("completed_files", []):
                    c_base, c_ext = os.path.splitext(completed_f)
                    if completed_f == expected_out_name or c_base.startswith(
                        f"{prefix}{base_name}_"
                    ):
                        already_done = True
                        break
                if already_done:
                    continue

            SYSTEM_STATE["current_file"] = filename
            SYSTEM_STATE["file_index_str"] = f"File {idx + 1} of {total_files}"
            SYSTEM_STATE["progress"] = 0
            SYSTEM_STATE["frames_done"] = 0
            SYSTEM_STATE["frames_total"] = 0
            SYSTEM_STATE["is_4k_plus"] = False
            SYSTEM_STATE["is_interpolating"] = False
            SYSTEM_STATE["is_remapped"] = reverse_remap_requested
            SYSTEM_STATE["text"] = "Analyzing media sequence..."

            video_path = os.path.join(_I_D_X, clean_file_name)
            if not os.path.isfile(video_path) or not _is_safe_path(video_path):
                raise ValueError("Input file is missing or invalid.")
            out_name = f"{prefix}{base_name}{ext}"
            output_path = os.path.join(_O_D_X, out_name)
            counter = 1
            while os.path.exists(output_path):
                out_name = f"{prefix}{base_name}_{counter}{ext}"
                output_path = os.path.join(_O_D_X, out_name)
                counter += 1

            drive_dest_path = None
            if config.get("saveToDrive"):
                drive_folder = "/content/drive/MyDrive/CrownScaler"
                os.makedirs(drive_folder, exist_ok=True)
                drive_dest_path = os.path.join(drive_folder, out_name)

            try:
                is_video = video_path.lower().endswith((".mp4", ".avi", ".mov", ".mkv"))

                if not is_video:
                    frame = cv2.imread(video_path, cv2.IMREAD_COLOR)
                    if frame is None:
                        raise RuntimeError("Failed to decode source image.")
                    height, width = frame.shape[:2]
                    fps = 30.0
                    total_frames = 1
                    SYSTEM_STATE["frames_total"] = 1
                    interp_multiplier = 1
                else:
                    cap = cv2.VideoCapture(video_path)
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    raw_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                    raw_frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                    if width == 0 or height == 0:
                        cap.release()
                        raise RuntimeError("Failed to read video dimensions.")

                    if fps_120_requested and rife_model is not None and raw_fps < 119.0:
                        interp_multiplier = max(2, int(round(120.0 / raw_fps)))
                        total_frames = max(1, (raw_frame_count - 1) * interp_multiplier + 1)
                        source_duration = max(raw_frame_count / raw_fps, 1.0 / raw_fps)
                        # The first source frame is retained rather than
                        # interpolated.  Use the actual emitted frame count so
                        # the video stays synchronized with its audio.
                        fps = total_frames / source_duration
                        SYSTEM_STATE["is_interpolating"] = True
                        if reverse_remap_requested:
                            SYSTEM_STATE["text"] = f"Remap + 120 FPS ({raw_fps:.1f}fps -> {fps:.1f}fps)"
                        else:
                            SYSTEM_STATE["text"] = f"120 FPS Enabled ({raw_fps:.1f}fps -> {fps:.1f}fps)"
                    else:
                        interp_multiplier = 1
                        fps = raw_fps
                        total_frames = raw_frame_count
                        SYSTEM_STATE["is_interpolating"] = False

                    if speed_factor != 1.0:
                        fps = fps * speed_factor

                    SYSTEM_STATE["frames_total"] = total_frames

                if width >= 3840 or height >= 2160:
                    SYSTEM_STATE["is_4k_plus"] = True

                out_width = width * scale_factor
                out_height = height * scale_factor

                target_res = str(config.get("resolution", "UPSCALED")).strip().lower()
                final_out_w, final_out_h = out_width, out_height

                if target_res == "1080p":
                    max_w, max_h = 1920, 1080
                    scale = min(max_w / out_width, max_h / out_height)
                    final_out_w = int(out_width * scale)
                    final_out_h = int(out_height * scale)
                elif target_res == "upscaled":
                    pass
                elif "x" in target_res:
                    try:
                        parsed_w, parsed_h = map(
                            int, target_res.replace(" ", "").split("x")
                        )
                        if not (0 < parsed_w <= 8192 and 0 < parsed_h <= 8192):
                            raise ValueError(
                                "Custom resolution dimensions out of bounds (1-8192)"
                            )
                        final_out_w, final_out_h = parsed_w, parsed_h
                    except ValueError as e:
                        raise RuntimeError(f"Invalid custom scale: {e}")

                final_out_w = (final_out_w // 2) * 2
                final_out_h = (final_out_h // 2) * 2

                temporal_frames = None
                if is_video and (reverse_remap_requested or remove_dead_requested):
                    SYSTEM_STATE["text"] = "Preparing temporal frame sequence..."
                    temporal_frames = []
                    while cap.isOpened():
                        ret, read_frame = cap.read()
                        if not ret or read_frame is None:
                            break
                        temporal_frames.append(read_frame)

                    if remove_dead_requested and len(temporal_frames) > 1:
                        temporal_frames = remove_dead_frames_list(
                            temporal_frames, threshold=dead_threshold
                        )
                    if not temporal_frames:
                        raise RuntimeError("No decodable frames were found in the source video.")

                    total_frames = (
                        max(1, (len(temporal_frames) - 1) * interp_multiplier + 1)
                        if interp_multiplier > 1 and rife_model is not None
                        else len(temporal_frames)
                    )
                    # Decimation changes the number of frames.  Derive the
                    # encoder rate from the original duration so duplicate
                    # removal does not accidentally shorten the video.
                    source_duration = max(
                        raw_frame_count / raw_fps, len(temporal_frames) / raw_fps
                    )
                    fps = (total_frames / source_duration) * speed_factor
                    SYSTEM_STATE["frames_total"] = total_frames

                cuda_stream = torch.cuda.current_stream()

                if not is_video:
                    SYSTEM_STATE["text"] = "Rendering Image..."
                    out_rgb_tensor = process_frame_tiled(
                        frame,
                        context,
                        engine,
                        input_name,
                        output_name,
                        input_torch_dtype,
                        out_torch_dtype,
                        cuda_stream,
                        scale_factor=scale_factor,
                        recover_details=recover_details_val,
                        sharpen_val=sharpen_setting,
                        denoise_val=denoise_setting,
                        dehalo_val=dehalo_setting,
                    )

                    out_rgb = (
                        out_rgb_tensor.mul(255.0)
                        .clamp(0, 255)
                        .to(torch.uint8)
                        .squeeze(0)
                        .permute(1, 2, 0)
                    )
                    out_bgr = out_rgb.flip(-1).contiguous().cpu().numpy()

                    if final_out_w != out_width or final_out_h != out_height:
                        out_bgr = cv2.resize(
                            out_bgr,
                            (final_out_w, final_out_h),
                            interpolation=cv2.INTER_LANCZOS4,
                        )

                    if not cv2.imwrite(output_path, out_bgr):
                        raise RuntimeError("Failed to encode/write output image")

                    SYSTEM_STATE["progress"] = 100
                    SYSTEM_STATE["frames_done"] = 1
                else:
                    vcodec = (
                        "hevc_nvenc"
                        if config.get("codec") == "H.265"
                        else "h264_nvenc"
                    )
                    nvenc_preset = config.get("preset", "p4")
                    crf_val = int(config.get("crfValue", 15))

                    ffmpeg_cmd = [
                        "ffmpeg",
                        "-y",
                        "-loglevel",
                        "error",
                        "-fflags",
                        "+genpts",
                        "-avoid_negative_ts",
                        "make_zero",
                        "-f",
                        "rawvideo",
                        "-vcodec",
                        "rawvideo",
                        "-pix_fmt",
                        "bgr24",
                        "-s",
                        f"{final_out_w}x{final_out_h}",
                        "-r",
                        str(fps),
                        "-i",
                        "-",
                    ]

                    if config.get("keepAudio", True) and not reverse_remap_requested:
                        ffmpeg_cmd.extend([
                            "-i",
                            video_path,
                            "-map",
                            "0:v:0",
                            "-map",
                            "1:a?",
                        ])
                        if speed_factor != 1.0:
                            ffmpeg_cmd.extend([
                                "-filter:a",
                                build_atempo_filter(speed_factor),
                            ])
                        ffmpeg_cmd.extend([
                            "-c:a",
                            "aac",
                            "-b:a",
                            "256k",
                            "-ar",
                            "48000",
                            "-ac",
                            "2",
                            "-shortest",
                        ])
                    else:
                        ffmpeg_cmd.extend(["-map", "0:v:0"])

                    ffmpeg_cmd.extend([
                        "-c:v",
                        vcodec,
                        "-preset",
                        nvenc_preset,
                        "-rc:v",
                        "vbr",
                        "-cq:v",
                        str(crf_val),
                        "-qmin",
                        str(max(1, crf_val - 2)),
                        "-qmax",
                        str(min(51, crf_val + 2)),
                        "-pix_fmt",
                        "yuv420p",
                    ])

                    if vcodec == "h264_nvenc":
                        ffmpeg_cmd.extend(["-profile:v", "high", "-level:v", "4.1"])

                    ffmpeg_cmd.extend([
                        "-movflags",
                        "+faststart",
                        output_path,
                    ])

                    process = subprocess.Popen(
                        ffmpeg_cmd,
                        stdin=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        bufsize=10**8,
                    )

                    stderr_chunks = []

                    def _drain_stderr():
                        for line in iter(process.stderr.readline, b""):
                            stderr_chunks.append(line)

                    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
                    stderr_thread.start()

                    input_queue = queue.Queue(maxsize=16)
                    output_queue = queue.Queue(maxsize=16)
                    stop_event = threading.Event()

                    def frame_reader():
                        need_cache = reverse_remap_requested or remove_dead_requested

                        if need_cache:
                            raw_frames_cache = temporal_frames
                            num_raw = len(raw_frames_cache)

                            prev_f = None
                            if reverse_remap_requested:
                                for k in range(num_raw):
                                    if stop_event.is_set():
                                        break
                                    percent_out = k / max(1, (num_raw - 1))
                                    percent_in = float(np.interp(percent_out, lut_x, lut_y))
                                    frame_idx = int(round(percent_in * (num_raw - 1)))
                                    frame_idx = max(0, min(num_raw - 1, frame_idx))
                                    read_frame = raw_frames_cache[frame_idx]

                                    if interp_multiplier > 1 and rife_model is not None:
                                        if prev_f is not None:
                                            intermediates = interpolate_pair_rife(
                                                rife_model, prev_f, read_frame, interp_multiplier
                                            )
                                            for mid in intermediates:
                                                if stop_event.is_set():
                                                    break
                                                while not stop_event.is_set():
                                                    try:
                                                        input_queue.put(mid, timeout=0.5)
                                                        break
                                                    except queue.Full:
                                                        pass

                                        while not stop_event.is_set():
                                            try:
                                                input_queue.put(read_frame, timeout=0.5)
                                                break
                                            except queue.Full:
                                                pass
                                        prev_f = read_frame
                                    else:
                                        while not stop_event.is_set():
                                            try:
                                                input_queue.put(read_frame, timeout=0.5)
                                                break
                                            except queue.Full:
                                                pass
                            else:
                                for k in range(num_raw):
                                    if stop_event.is_set():
                                        break
                                    read_frame = raw_frames_cache[k]
                                    if interp_multiplier > 1 and rife_model is not None:
                                        if prev_f is not None:
                                            intermediates = interpolate_pair_rife(
                                                rife_model, prev_f, read_frame, interp_multiplier
                                            )
                                            for mid in intermediates:
                                                if stop_event.is_set():
                                                    break
                                                while not stop_event.is_set():
                                                    try:
                                                        input_queue.put(mid, timeout=0.5)
                                                        break
                                                    except queue.Full:
                                                        pass

                                        while not stop_event.is_set():
                                            try:
                                                input_queue.put(read_frame, timeout=0.5)
                                                break
                                            except queue.Full:
                                                pass
                                        prev_f = read_frame
                                    else:
                                        while not stop_event.is_set():
                                            try:
                                                input_queue.put(read_frame, timeout=0.5)
                                                break
                                            except queue.Full:
                                                pass
                        else:
                            prev_f = None
                            while cap.isOpened() and not stop_event.is_set():
                                ret, read_frame = cap.read()
                                if not ret or read_frame is None:
                                    break

                                if interp_multiplier > 1 and rife_model is not None:
                                    if prev_f is not None:
                                        intermediates = interpolate_pair_rife(
                                            rife_model, prev_f, read_frame, interp_multiplier
                                        )
                                        for mid in intermediates:
                                            if stop_event.is_set():
                                                break
                                            while not stop_event.is_set():
                                                try:
                                                    input_queue.put(mid, timeout=0.5)
                                                    break
                                                except queue.Full:
                                                    pass

                                    while not stop_event.is_set():
                                        try:
                                            input_queue.put(read_frame, timeout=0.5)
                                            break
                                        except queue.Full:
                                            pass
                                    prev_f = read_frame
                                else:
                                    while not stop_event.is_set():
                                        try:
                                            input_queue.put(read_frame, timeout=0.5)
                                            break
                                        except queue.Full:
                                            pass

                        while not stop_event.is_set():
                            try:
                                input_queue.put(None, timeout=0.5)
                                break
                            except queue.Full:
                                pass

                    def frame_writer():
                        while not stop_event.is_set():
                            try:
                                item = output_queue.get(timeout=0.5)
                            except queue.Empty:
                                continue
                            if item is None:
                                break
                            try:
                                process.stdin.write(item)
                            except Exception:
                                stop_event.set()
                                break

                    reader_thread = threading.Thread(target=frame_reader, daemon=True)
                    writer_thread = threading.Thread(target=frame_writer, daemon=True)
                    reader_thread.start()
                    writer_thread.start()

                    processed_frames = 0
                    upscaled_motion_history = []
                    SYSTEM_STATE["text"] = "Warming up CUDA cores..."

                    try:
                        while IS_RUNNING and not stop_event.is_set():
                            try:
                                in_frame = input_queue.get(timeout=0.5)
                            except queue.Empty:
                                continue
                            if in_frame is None:
                                break

                            if processed_frames == 0:
                                if reverse_remap_requested and interp_multiplier > 1:
                                    SYSTEM_STATE["text"] = "Remapping + Interpolating + Upscaling..."
                                elif reverse_remap_requested:
                                    SYSTEM_STATE["text"] = "Remapping + Upscaling Output..."
                                elif interp_multiplier > 1:
                                    SYSTEM_STATE["text"] = "Interpolating + Upscaling Output..."
                                else:
                                    SYSTEM_STATE["text"] = "Rendering Output..."

                            upscaled_gpu_tensor = process_frame_tiled(
                                in_frame,
                                context,
                                engine,
                                input_name,
                                output_name,
                                input_torch_dtype,
                                out_torch_dtype,
                                cuda_stream,
                                scale_factor=scale_factor,
                                recover_details=recover_details_val,
                                sharpen_val=sharpen_setting,
                                denoise_val=denoise_setting,
                                dehalo_val=dehalo_setting,
                            )

                            if rsmb_blur_val > 0 and upscaled_gpu_tensor is not None:
                                final_tensor = apply_realtime_motion_blur_gpu(
                                    upscaled_gpu_tensor,
                                    upscaled_motion_history,
                                    blur_val=rsmb_blur_val,
                                    shutter_mult=shutter_multiplier,
                                )
                                upscaled_motion_history.append(upscaled_gpu_tensor.detach())
                                if len(upscaled_motion_history) > 4:
                                    upscaled_motion_history.pop(0)
                            else:
                                final_tensor = upscaled_gpu_tensor

                            out_rgb = (
                                final_tensor.mul(255.0)
                                .clamp(0, 255)
                                .to(torch.uint8)
                                .squeeze(0)
                                .permute(1, 2, 0)
                            )
                            out_bgr = out_rgb.flip(-1).contiguous().cpu().numpy()

                            if final_out_w != out_width or final_out_h != out_height:
                                out_bgr = cv2.resize(
                                    out_bgr,
                                    (final_out_w, final_out_h),
                                    interpolation=cv2.INTER_LANCZOS4,
                                )

                            out_bytes = np.ascontiguousarray(out_bgr).tobytes()

                            while IS_RUNNING and not stop_event.is_set():
                                try:
                                    output_queue.put(out_bytes, timeout=0.5)
                                    break
                                except queue.Full:
                                    pass
                            processed_frames += 1

                            SYSTEM_STATE["frames_done"] = processed_frames

                            if total_frames > 0:
                                pct = int((processed_frames / total_frames) * 100)
                                SYSTEM_STATE["progress"] = min(100, pct)
                            else:
                                SYSTEM_STATE["progress"] = 0

                        while IS_RUNNING and not stop_event.is_set():
                            try:
                                output_queue.put(None, timeout=0.5)
                                break
                            except queue.Full:
                                pass

                    finally:
                        stop_event.set()
                        reader_thread.join(timeout=2.0)
                        writer_thread.join(timeout=2.0)
                        cap.release()

                        try:
                            process.stdin.close()
                        except Exception:
                            pass

                        try:
                            process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                            raise RuntimeError("FFmpeg Crash: Video encoding timed out")

                        stderr_thread.join(timeout=5)
                        if process.returncode != 0:
                            ffmpeg_err = b"".join(stderr_chunks).decode(
                                "utf-8", errors="ignore"
                            )
                            raise RuntimeError(f"FFmpeg Crash: {ffmpeg_err}")

                    if config.get("fixIphoneTag"):
                        try:
                            if os.path.exists(output_path):
                                temp_path = output_path + ".tagfix.mp4"
                                tag_cmd = [
                                    "ffmpeg",
                                    "-y",
                                    "-i",
                                    output_path,
                                    "-c",
                                    "copy",
                                    "-movflags",
                                    "+faststart",
                                ]
                                if vcodec == "hevc_nvenc":
                                    tag_cmd.extend(["-tag:v", "hvc1"])
                                tag_cmd.extend(["-f", "mp4", temp_path])

                                res_tag = subprocess.run(
                                    tag_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                                )
                                if res_tag.returncode == 0 and os.path.exists(temp_path):
                                    os.replace(temp_path, output_path)
                                elif os.path.exists(temp_path):
                                    os.remove(temp_path)
                        except Exception:
                            if "temp_path" in locals() and os.path.exists(temp_path):
                                try:
                                    os.remove(temp_path)
                                except Exception:
                                    pass

                if not IS_RUNNING:
                    if os.path.exists(output_path):
                        try:
                            os.remove(output_path)
                        except Exception:
                            pass
                    break

                if (
                    config.get("saveToDrive")
                    and os.path.exists(output_path)
                    and drive_dest_path
                ):
                    shutil.copy(output_path, drive_dest_path)
                    DRIVE_SAVED_FILES.add(output_path)

                SYSTEM_STATE["completed_files"].append(out_name)
                if out_name not in pre_recovery_completed:
                    SYSTEM_STATE["new_completed_files"].append(out_name)
                SYSTEM_STATE["last_completed"] = out_name

                if os.path.exists(_REC_FILE):
                    try:
                        with open(_REC_FILE, "r") as f:
                            rec_data = json.load(f)
                        rec_data["completed_files"] = SYSTEM_STATE["completed_files"]
                        with open(_REC_FILE, "w") as f:
                            json.dump(rec_data, f)
                    except Exception:
                        pass

                update_file_states()

                torch.cuda.empty_cache()
                gc.collect()

            except Exception as file_err:
                traceback.print_exc()
                SYSTEM_STATE["unsupported_files"][filename] = str(file_err)
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except Exception:
                        pass
                continue

        if IS_RUNNING:
            SYSTEM_STATE["status"] = "complete"
            SYSTEM_STATE["progress"] = 100
            if os.path.exists(_REC_FILE):
                try:
                    os.remove(_REC_FILE)
                except Exception:
                    pass

    except Exception as e:
        traceback.print_exc()
        trace = traceback.format_exc()
        SYSTEM_STATE["status"] = "error"
        SYSTEM_STATE["error_log"] = trace
        if os.path.exists(_REC_FILE):
            try:
                os.remove(_REC_FILE)
            except Exception:
                pass
        if "output_path" in locals() and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
    finally:
        gpu_cleanup(engine, context, rife_model)
        IS_RUNNING = False


# ------------------------------------------------------------------------------
# ZIP CREATION FUNCTION
# ------------------------------------------------------------------------------
def create_zip(file_list=None):
    from datetime import datetime
    import zipfile

    if not os.path.exists(_O_D_X):
        return {"success": False, "error": "No output directory"}
    all_files = []
    for f in os.listdir(_O_D_X):
        if f.startswith(".") or f.lower().endswith(".zip"):
            continue
        p = os.path.join(_O_D_X, f)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            all_files.append(p)
    if file_list:
        files_to_zip = []
        for fp in file_list:
            if not _is_safe_path(fp) or not os.path.isfile(fp):
                continue
            files_to_zip.append(fp)
    else:
        files_to_zip = all_files
    if not files_to_zip:
        return {"success": False, "error": "No files to zip"}
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"Crownscaled_{timestamp}.zip"
    zip_path = os.path.join(_O_D_X, zip_name)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in files_to_zip:
                arcname = os.path.basename(fp)
                zf.write(fp, arcname=arcname)
        _invalidate_thumb_cache(zip_path)
        update_file_states()
        return {"success": True, "zip_path": zip_path}
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": str(e)}


def handle_cmd(data_str):
    global IS_RUNNING, PIPELINE_THREAD, SYSTEM_STATE
    config = json.loads(data_str)

    if config.get("command") == "INIT_SETUP":
        return

    if config.get("command") == "DISCARD_RECOVERY":
        if os.path.exists(_REC_FILE):
            try:
                os.remove(_REC_FILE)
            except Exception:
                pass
        SYSTEM_STATE["recovery_config"] = None
        SYSTEM_STATE["completed_files"] = []
        SYSTEM_STATE["new_completed_files"] = []
        return IPython.display.JSON({"success": True})

    if config.get("command") == "REFRESH":
        try:
            update_file_states()
        except Exception:
            pass
        return IPython.display.JSON({"success": True})

    if config.get("command") == "DELETE":
        try:
            target = config.get("file")
            if not target or not _is_safe_path(target):
                return IPython.display.JSON({"success": False, "error": "Invalid path"})
            if os.path.exists(target):
                os.remove(target)
                _invalidate_thumb_cache(target)
            update_file_states()
            return IPython.display.JSON({"success": True})
        except Exception:
            return IPython.display.JSON({"success": False})

    if config.get("command") == "MOUNT_DRIVE":
        try:
            from google.colab import drive

            if not os.path.ismount("/content/drive"):
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    drive.mount("/content/drive")
            return IPython.display.JSON({"success": True})
        except Exception as e:
            return IPython.display.JSON({"success": False, "error": str(e)})

    if config.get("command") == "SAVE_TO_DRIVE":
        try:
            from google.colab import drive

            if not os.path.ismount("/content/drive"):
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    drive.mount("/content/drive")

            file_path = config.get("file")
            if not file_path or not _is_safe_path(file_path) or not os.path.exists(file_path):
                return IPython.display.JSON({"success": False, "error": "File does not exist or invalid path"})

            drive_folder = "/content/drive/MyDrive/CrownScaler"
            os.makedirs(drive_folder, exist_ok=True)

            clean_name = sanitize_filename(os.path.basename(file_path))
            base_name, ext = os.path.splitext(clean_name)
            dest_path = os.path.join(drive_folder, f"{base_name}{ext}")
            counter = 1
            while os.path.exists(dest_path):
                dest_path = os.path.join(
                    drive_folder, f"{base_name}_{counter}{ext}"
                )
                counter += 1

            shutil.copy(file_path, dest_path)
            DRIVE_SAVED_FILES.add(file_path)
            update_file_states()
            return IPython.display.JSON({"success": True})
        except Exception as e:
            return IPython.display.JSON({"success": False, "error": str(e)})

    if config.get("command") == "STOP":
        IS_RUNNING = False
        SYSTEM_STATE["status"] = "idle"
        if os.path.exists(_REC_FILE):
            try:
                os.remove(_REC_FILE)
            except Exception:
                pass
        return

    if config.get("command") == "CREATE_ZIP":
        result = create_zip(config.get("files"))
        return IPython.display.JSON(result)

    if config.get("command") == "HEARTBEAT":
        SYSTEM_STATE["last_heartbeat"] = time.time()
        return IPython.display.JSON({"success": True})

    if not IS_RUNNING and config.get("command") == "START":
        try:
            with open(_REC_FILE, "w") as f:
                json.dump(config, f)
        except Exception:
            pass

        IS_RUNNING = True
        SYSTEM_STATE["status"] = "running"
        SYSTEM_STATE["progress"] = 0
        SYSTEM_STATE["frames_done"] = 0
        SYSTEM_STATE["frames_total"] = 0
        SYSTEM_STATE["is_4k_plus"] = False
        SYSTEM_STATE["is_interpolating"] = False
        SYSTEM_STATE["is_remapped"] = bool(config.get("reverseRemap", False))
        SYSTEM_STATE["text"] = "Initializing Pipeline..."
        SYSTEM_STATE["error_log"] = ""
        SYSTEM_STATE["unsupported_files"] = {}

        if config.get("is_recovery"):
            completed = []
            if os.path.exists(_REC_FILE):
                try:
                    with open(_REC_FILE, "r") as f:
                        rec_data = json.load(f)
                    completed = rec_data.get("completed_files", [])
                except Exception:
                    pass
            if not completed and "completed_files" in config:
                completed = config.get("completed_files", [])
            SYSTEM_STATE["completed_files"] = completed
            SYSTEM_STATE["new_completed_files"] = []
        else:
            SYSTEM_STATE["completed_files"] = []
            SYSTEM_STATE["new_completed_files"] = []

        PIPELINE_THREAD = threading.Thread(
            target=run_pipeline, args=(config,), daemon=True
        )
        PIPELINE_THREAD.start()


output.register_callback("notebook.command", handle_cmd)

# ==============================================================================
# 8. UI FRONTEND
# ==============================================================================
html_ui = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link href="https://fonts.googleapis.com/icon?family=Material+Icons+Round" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.2/css/all.min.css">
<style>
    :root {
        --bg-amoled: #0B0C10;
        --surface-card: #14151C;
        --surface-inner: #1C1E26;
        --border-distinct: #2D313D;
        --primary-yellow: #FFD600;
        --dark-yellow: #E6C200;
        --highlight-cta: #EF4444;
        --text-primary: #FFFFFF;
        --text-secondary: #E4E5EB;
        --text-muted: #A0A5B5;
        --card-glow: rgba(255, 214, 0, 0.08);
        --card-shadow: 0 14px 40px rgba(0, 0, 0, 0.6);
        --modal-bg: #14151C;
        --chip-text: #FFFFFF;
        --input-bg: #1C1E26;
        --btn-text-on-accent: #000000;
        --font: 'Plus Jakarta Sans', sans-serif;
    }

    [data-theme="dark"] {
        --bg-amoled: #0B0C10;
        --surface-card: #14151C;
        --surface-inner: #1C1E26;
        --border-distinct: #2D313D;
        --primary-yellow: #FFD600;
        --dark-yellow: #E6C200;
        --highlight-cta: #EF4444;
        --text-primary: #FFFFFF;
        --text-secondary: #E4E5EB;
        --text-muted: #A0A5B5;
        --card-glow: rgba(255, 214, 0, 0.08);
        --card-shadow: 0 14px 40px rgba(0, 0, 0, 0.6);
        --modal-bg: #14151C;
        --chip-text: #FFFFFF;
        --input-bg: #1C1E26;
        --btn-text-on-accent: #000000;
    }

    [data-theme="light"] {
        --bg-amoled: #F5F7FA;
        --surface-card: #FFFFFF;
        --surface-inner: #F0F2F6;
        --border-distinct: #DCE1E8;
        --primary-yellow: #EAB308;
        --dark-yellow: #CA8A04;
        --highlight-cta: #DC2626;
        --text-primary: #0F172A;
        --text-secondary: #334155;
        --text-muted: #64748B;
        --card-glow: rgba(234, 179, 8, 0.12);
        --card-shadow: 0 6px 20px rgba(0, 0, 0, 0.06), 0 1px 3px rgba(0, 0, 0, 0.03);
        --modal-bg: #FFFFFF;
        --chip-text: #0F172A;
        --input-bg: #F8FAFC;
        --btn-text-on-accent: #000000;
    }

    html, body {
        background-color: var(--bg-amoled);
        color: var(--text-primary);
        font-family: var(--font);
        margin: 0;
        padding: 0;
        width: 100%;
        height: 100%;
        -webkit-font-smoothing: antialiased;
        transition: background-color 0.25s ease, color 0.25s ease;
    }

    .md3-spinner { animation: md3-rotate 2s linear infinite; transform-origin: center center; width: 56px; height: 56px; margin-bottom: 24px; }
    .md3-spinner circle { stroke: var(--primary-yellow); stroke-width: 4; stroke-dasharray: 1, 200; stroke-dashoffset: 0; animation: md3-dash 1.5s ease-in-out infinite; stroke-linecap: round; fill: none; }
    @keyframes md3-rotate { 100% { transform: rotate(360deg); } }
    @keyframes md3-dash {
        0% { stroke-dasharray: 1, 200; stroke-dashoffset: 0; }
        50% { stroke-dasharray: 89, 200; stroke-dashoffset: -35px; }
        100% { stroke-dasharray: 89, 200; stroke-dashoffset: -124px; }
    }

    .fa-spin { animation: fa-spin 1s infinite linear; }
    @keyframes fa-spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(359deg); } }

    .wavy-progress-bg { width: 220px; height: 6px; background: var(--surface-inner); border-radius: 10px; overflow: hidden; position: relative; margin-bottom: 8px; border: 1px solid var(--border-distinct); }
    .wavy-progress-fill { height: 100%; width: 0%; background: repeating-linear-gradient(45deg, var(--primary-yellow) 0%, var(--dark-yellow) 25%, var(--primary-yellow) 50%); background-size: 200% 100%; animation: wave-move 2s linear infinite; transition: width 0.3s ease; border-radius: 10px; }
    @keyframes wave-move { 0% { background-position: 100% 0; } 100% { background-position: -100% 0; } }

    #startupLoader { width: 100%; min-height: 400px; background: var(--surface-card); border: 1px solid var(--border-distinct); border-radius: 24px; display: flex; flex-direction: column; align-items: center; justify-content: center; transition: opacity 0.5s ease, background 0.25s ease; margin: 20px 0; padding: 40px; box-sizing: border-box; box-shadow: var(--card-shadow); }
    .loader-title { font-size: 20px; font-weight: 800; margin-bottom: 8px; text-align:center; color: var(--text-primary); }
    .loader-subtitle { font-size: 13px; color: var(--text-muted); margin-bottom: 24px; text-align:center; }
    .loader-timer { font-size: 12px; font-weight: 700; color: var(--primary-yellow); font-variant-numeric: tabular-nums; margin-bottom: 12px; }

    .studio-canvas { width: 100%; max-width: 1200px; margin: 0 auto; padding: 16px; padding-bottom: 120px; box-sizing: border-box; display: none; flex-direction: column; gap: 16px; }
    .dashboard-layout { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr)); gap: 16px; width: 100%; box-sizing: border-box; }
    @media (min-width: 1024px) { .span-full { grid-column: 1 / -1; } }

    .view-section { display: none; flex-direction: column; gap: 16px; width: 100%; box-sizing: border-box; opacity: 0; transform: translateY(15px); }
    .view-section.active-view { display: flex; animation: slideFadeIn 0.35s cubic-bezier(0.2, 0.8, 0.2, 1) forwards; }
    @keyframes slideFadeIn { to { opacity: 1; transform: translateY(0); } }

    .app-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 4px; border-bottom: 1px solid var(--border-distinct); margin-bottom: 4px; position: relative; }
    .brand-link-wrapper { display: flex; align-items: center; gap: 12px; text-decoration: none; cursor: pointer; transition: transform 0.2s; }
    .brand-link-wrapper:active { transform: scale(0.9) translateY(2px); }
    .app-title-main { font-size: 22px; font-weight: 800; letter-spacing: -0.5px; color: var(--text-primary); margin: 0; }
    .app-title-main span { color: var(--primary-yellow); }
    .app-title-sub { font-size: 18px; font-weight: 800; color: var(--text-primary); margin: 0; margin-left: 4px; }

    .social-symbol { display: flex; align-items: center; justify-content: center; background-color: var(--surface-inner); border: 1px solid var(--border-distinct); width: 34px; height: 34px; border-radius: 50%; color: var(--text-primary); text-decoration: none; transition: 0.2s; }
    .social-symbol:hover { border-color: var(--primary-yellow); }
    .social-symbol.yt i { color: #FF0000; font-size: 16px; }
    .social-symbol.dc i { color: #5865F2; font-size: 16px; }
    .social-symbol.wb i { color: #10B981; font-size: 16px; }

    .icon-btn { color: var(--primary-yellow); cursor: pointer; font-size: 22px; transition: 0.2s; }

    .notify-dropdown { position: absolute; right: 0; top: 50px; background: var(--surface-card); border: 1px solid var(--border-distinct); border-radius: 16px; padding: 16px; width: 240px; box-shadow: var(--card-shadow); z-index: 100; display: none; flex-direction: column; gap: 12px; }
    .notify-dropdown.show { display: flex; animation: slideFadeIn 0.2s forwards; }

    .compose-card {
        position: relative;
        background: var(--surface-card);
        border: 1px solid var(--border-distinct);
        box-shadow: var(--card-shadow);
        border-radius: 28px;
        padding: 20px;
        display: flex;
        flex-direction: column;
        gap: 14px;
        box-sizing: border-box;
        width: 100%;
        transition: background 0.25s ease, border-color 0.25s ease;
    }
    .component-label-group { display: flex; align-items: center; gap: 8px; }
    .component-label-group .icon { font-size: 18px; color: var(--primary-yellow); }
    .component-title { font-size: 14px; font-weight: 700; margin: 0; color: var(--text-primary); }

    .upload-box { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 22px 8px; text-align: center; border: 2px dashed var(--border-distinct); border-radius: 22px; transition: 0.2s; cursor: pointer; background: var(--surface-inner); }
    .upload-box:hover { border-color: var(--primary-yellow); background: var(--card-glow); }
    .upload-box.drag-over { border-color: var(--primary-yellow); background: var(--card-glow); }
    .upload-circle { width: 50px; height: 50px; border-radius: 50%; background-color: var(--surface-card); display: flex; align-items: center; justify-content: center; margin-bottom: 10px; border: 1px solid var(--border-distinct); }
    .upload-circle .icon { color: var(--primary-yellow); font-size: 22px; }
    .upload-title { font-size: 15px; font-weight: 700; color: var(--text-primary); margin-bottom: 4px; }
    .upload-subtitle { font-size: 12px; color: var(--text-muted); }

    /* Pill-Shaped Link Import Bar */
    .url-import-container {
        display: flex;
        align-items: center;
        gap: 8px;
        width: 100%;
        box-sizing: border-box;
        margin-top: 6px;
    }
    .url-import-input {
        flex: 1;
        background-color: var(--surface-inner);
        border: 1px solid var(--border-distinct);
        border-radius: 100px;
        padding: 12px 20px;
        color: var(--text-primary);
        font-family: var(--font);
        font-size: 13px;
        outline: none;
        box-sizing: border-box;
        transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }
    .url-import-input:focus {
        border-color: var(--primary-yellow);
        box-shadow: 0 0 0 2px var(--card-glow);
    }
    .url-import-btn {
        background: var(--surface-inner);
        border: 1px solid var(--border-distinct);
        border-radius: 100px;
        padding: 12px 22px;
        color: var(--primary-yellow);
        font-family: var(--font);
        font-size: 13px;
        font-weight: 800;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        transition: all 0.2s ease;
        flex-shrink: 0;
        box-sizing: border-box;
    }
    .url-import-btn:hover {
        background: var(--primary-yellow);
        color: #000000;
        border-color: var(--primary-yellow);
        box-shadow: 0 4px 14px var(--card-glow);
    }

    .file-stack { display: flex; flex-direction: column; gap: 8px; margin-top: 4px; }

    .preview-panel {
        background: var(--surface-inner);
        border: 1px solid var(--border-distinct);
        border-radius: 18px;
        padding: 10px 14px;
        display: flex;
        align-items: center;
        gap: 16px;
        width: 100%;
        box-sizing: border-box;
        position: relative;
        transition: all 0.25s ease;
    }
    .preview-panel:hover {
        border-color: var(--primary-yellow);
    }
    .preview-panel.unsupported {
        opacity: 0.75;
        border-color: var(--highlight-cta);
    }

    .file-checkbox-wrapper { display: flex; align-items: center; justify-content: center; }
    .file-checkbox { -webkit-appearance: none; appearance: none; width: 24px; height: 24px; border-radius: 50%; border: 2px solid var(--border-distinct); background: var(--surface-card); cursor: pointer; position: relative; transition: all 0.2s ease; margin: 0; display: inline-block; flex-shrink: 0; }
    .file-checkbox:checked { background-color: var(--primary-yellow); border-color: var(--primary-yellow); }
    .file-checkbox:checked::after { content: ''; position: absolute; left: 7px; top: 3px; width: 5px; height: 10px; border: solid #000000; border-width: 0 2.5px 2.5px 0; transform: rotate(45deg); }

    .preview-media-container { width: 54px; height: 54px; border-radius: 12px; background-color: var(--surface-card); overflow: hidden; flex-shrink: 0; display: flex; align-items: center; justify-content: center; cursor: pointer; border: 1px solid var(--border-distinct); }
    .preview-media-container img, .preview-media-container video, .media-avatar-container img, .media-avatar-container video { width: auto !important; height: auto !important; max-width: 100%; max-height: 100%; object-fit: contain; pointer-events: none; }

    .preview-details { display: flex; flex-direction: column; gap: 4px; overflow: hidden; width: 100%; }
    .preview-filename { font-size: 13px; font-weight: 700; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; width: 100%; cursor: pointer; }
    .meta-sub-metrics { font-size: 11px; color: var(--text-muted); font-weight: 600; }
    .cancel-upload-btn { position: absolute; right: 14px; color: var(--text-muted); cursor: pointer; font-size: 20px; transition: 0.2s; }
    .cancel-upload-btn:hover { color: var(--highlight-cta); }

    .action-button-main { width: 100%; background: linear-gradient(135deg, var(--primary-yellow), var(--dark-yellow)); color: var(--btn-text-on-accent); border: none; padding: 18px; border-radius: 100px; font-family: var(--font); font-size: 16px; font-weight: 800; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 10px; box-shadow: 0 4px 18px var(--card-glow); box-sizing: border-box; margin: 4px 0; transition: 0.2s; }

    .flex-row-grid { display: flex; gap: 12px; width: 100%; box-sizing: border-box; }
    .flex-col-grid { flex: 1; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
    .chip-container { display: flex; gap: 8px; flex-wrap: nowrap; overflow-x: auto; width: 100%; padding-bottom: 4px; -webkit-overflow-scrolling: touch; }
    .chip-container::-webkit-scrollbar { display: none; }
    .selectable-chip { padding: 10px 20px; border-radius: 100px; font-size: 13px; font-weight: 700; border: 1px solid var(--border-distinct); background-color: var(--surface-inner); color: var(--chip-text); cursor: pointer; flex-shrink: 0; transition: all 0.2s ease; }
    .selectable-chip.active { background-color: var(--primary-yellow); color: var(--btn-text-on-accent); border-color: var(--primary-yellow); font-weight: 800; }
    .block-selector { width: 100%; padding: 12px; border-radius: 100px; font-family: var(--font); font-size: 13px; font-weight: 700; background-color: var(--surface-inner); color: var(--chip-text); border: 1px solid var(--border-distinct); cursor: pointer; text-align: center; box-sizing: border-box; transition: all 0.2s ease; }
    .block-selector.active { background-color: var(--primary-yellow); color: var(--btn-text-on-accent); border-color: var(--primary-yellow); font-weight: 800; }

    .slider-info-row { display: flex; justify-content: space-between; align-items: center; width: 100%; }
    .badge-pills { font-size: 14px; font-weight: 800; color: var(--primary-yellow); }
    .range-container { position: relative; width: 100%; padding: 6px 0; }
    .range-input { -webkit-appearance: none; width: 100%; height: 6px; background: var(--surface-inner); border-radius: 100px; outline: none; border: 1px solid var(--border-distinct); }
    .range-input::-webkit-slider-thumb { -webkit-appearance: none; width: 22px; height: 22px; border-radius: 50%; background: var(--primary-yellow); cursor: pointer; box-shadow: 0 2px 6px rgba(0,0,0,0.2); }
    .slider-labels { display: flex; justify-content: space-between; font-size: 12px; font-weight: 600; color: var(--text-muted); }

    .switch-row { display: flex; justify-content: space-between; align-items: center; padding: 2px 0; gap: 8px; }
    .switch-widget { position: relative; display: inline-block; width: 44px; height: 24px; flex-shrink: 0; }
    .switch-widget input { opacity: 0; width: 0; height: 0; }
    .switch-slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: var(--surface-inner); transition: 0.2s; border-radius: 34px; border: 1px solid var(--border-distinct); }
    .switch-slider:before { position: absolute; content: ""; height: 16px; width: 16px; left: 3px; bottom: 3px; background-color: var(--text-muted); transition: 0.2s; border-radius: 50%; }
    input:checked + .switch-slider { background-color: var(--primary-yellow); border-color: var(--primary-yellow); }
    input:checked + .switch-slider:before { transform: translateX(20px); background-color: #000000; }

    .custom-select { background-color: var(--surface-inner); border: 1px solid var(--border-distinct); color: var(--text-primary); border-radius: 12px; padding: 6px 12px; font-family: var(--font); font-size: 13px; font-weight: 600; outline: none; cursor: pointer; }
    .custom-select:focus { border-color: var(--primary-yellow); }

    /* Dropdown Drawers */
    .custom-model-drawer, .remap-drawer, .fps-drawer {
        display: none;
        flex-direction: column;
        gap: 12px;
        background: var(--surface-inner);
        border: 1px dashed var(--border-distinct);
        border-radius: 18px;
        padding: 16px;
        margin-top: 6px;
        animation: slideFadeIn 0.25s ease forwards;
        box-sizing: border-box;
        width: 100%;
    }
    .custom-model-drawer.expanded, .remap-drawer.expanded, .fps-drawer.expanded {
        display: flex;
    }
    .model-upload-btn {
        background: var(--surface-card);
        border: 1px solid var(--border-distinct);
        border-radius: 14px;
        padding: 10px 16px;
        color: var(--text-primary);
        font-family: var(--font);
        font-size: 13px;
        font-weight: 700;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        transition: all 0.2s ease;
    }
    .model-upload-btn:hover {
        border-color: var(--primary-yellow);
        color: var(--primary-yellow);
    }
    .model-input-link {
        background-color: var(--surface-card);
        border: 1px solid var(--border-distinct);
        border-radius: 14px;
        padding: 10px 14px;
        color: var(--text-primary);
        font-family: var(--font);
        font-size: 13px;
        outline: none;
        box-sizing: border-box;
        width: 100%;
        transition: border-color 0.2s ease;
    }
    .model-input-link:focus {
        border-color: var(--primary-yellow);
    }

    .drawer-chevron-btn {
        cursor: pointer;
        font-size: 18px;
        color: var(--text-muted);
        transition: transform 0.25s ease, color 0.2s ease;
        display: flex;
        align-items: center;
        justify-content: center;
        user-select: none;
    }
    .drawer-chevron-btn:hover {
        color: var(--primary-yellow);
    }
    .drawer-chevron-btn.rotated {
        transform: rotate(180deg);
        color: var(--primary-yellow);
    }

    .modern-render-card {
        background: var(--surface-card);
        position: relative;
        overflow: hidden;
        padding: 40px 20px;
        display: flex;
        flex-direction: column;
        align-items: center;
        border: 1px solid var(--border-distinct);
    }

    .modern-glow-bg {
        position: absolute;
        top: -50%; left: -50%; width: 200%; height: 200%;
        background: radial-gradient(circle, var(--card-glow) 0%, transparent 50%);
        animation: pulse-glow 4s infinite alternate;
        z-index: 0;
        pointer-events: none;
    }
    @keyframes pulse-glow {
        0% { transform: scale(0.8); opacity: 0.5; }
        100% { transform: scale(1.1); opacity: 1; }
    }

    .modern-render-header {
        z-index: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 8px;
        margin-bottom: 32px;
    }
    .render-title { font-size: 20px; font-weight: 800; color: var(--text-primary); margin: 0; text-align: center; }
    .render-subtitle { font-size: 13px; color: var(--text-muted); text-align: center; }
    .pulse-icon {
        font-size: 32px;
        color: var(--primary-yellow);
        animation: float 3s ease-in-out infinite;
    }
    @keyframes float {
        0%, 100% { transform: translateY(0); }
        50% { transform: translateY(-5px); filter: drop-shadow(0 5px 10px var(--card-glow)); }
    }

    .modern-progress-wrapper {
        z-index: 1;
        width: 100%;
        max-width: 380px;
        text-align: center;
        margin-bottom: 32px;
    }
    .percentage-display {
        font-size: 48px;
        font-weight: 800;
        color: var(--text-primary);
        font-variant-numeric: tabular-nums;
        letter-spacing: -2px;
        margin-bottom: 12px;
    }
    .modern-bar-bg {
        width: 100%;
        height: 16px;
        background: var(--surface-inner);
        border: 1px solid var(--border-distinct);
        border-radius: 100px;
        position: relative;
        padding: 3px;
        box-sizing: border-box;
    }
    .modern-bar-fill {
        height: 100%;
        width: 0%;
        background: linear-gradient(90deg, var(--primary-yellow), var(--dark-yellow));
        border-radius: 100px;
        transition: width 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        position: relative;
        overflow: hidden;
    }
    .modern-bar-fill::after {
        content: "";
        position: absolute;
        top: 0; left: 0; right: 0; bottom: 0;
        background: linear-gradient(90deg, transparent, rgba(255,255,255,0.4), transparent);
        animation: shimmer 1.5s infinite linear;
    }
    @keyframes shimmer {
        0% { transform: translateX(-100%); }
        100% { transform: translateX(100%); }
    }

    .modern-bar-fill.warmup-pulse {
        width: 100% !important;
        background: repeating-linear-gradient(45deg, var(--surface-inner) 0%, var(--border-distinct) 50%, var(--surface-inner) 100%) !important;
        background-size: 200% 100% !important;
        animation: warmup-wave 1.5s infinite linear !important;
    }
    @keyframes warmup-wave { 0% { background-position: 100% 0; } 100% { background-position: -100% 0; } }

    .modern-stats-grid {
        z-index: 1;
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 12px;
        width: 100%;
        max-width: 380px;
    }
    .modern-stat-box {
        background: var(--surface-inner);
        border: 1px solid var(--border-distinct);
        border-radius: 18px;
        padding: 14px;
        display: flex;
        align-items: center;
        gap: 12px;
    }
    .modern-stat-box.span-2 {
        grid-column: 1 / -1;
    }
    .modern-stat-icon {
        width: 40px;
        height: 40px;
        border-radius: 12px;
        background: var(--card-glow);
        color: var(--primary-yellow);
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 20px;
        flex-shrink: 0;
        border: 1px solid var(--border-distinct);
    }
    .modern-stat-info {
        display: flex;
        flex-direction: column;
        gap: 2px;
    }
    .stat-label { font-size: 11px; font-weight: 600; color: var(--text-muted); }
    .stat-value { font-size: 14px; font-weight: 700; color: var(--text-primary); font-variant-numeric: tabular-nums; }

    .cancel-render-btn {
        background: rgba(239, 68, 68, 0.08);
        border: 1px solid rgba(239, 68, 68, 0.3);
        color: var(--highlight-cta);
        padding: 14px 32px;
        border-radius: 100px;
        font-family: var(--font);
        font-size: 14px;
        font-weight: 800;
        letter-spacing: 0.5px;
        text-transform: uppercase;
        cursor: pointer;
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        z-index: 1;
        position: relative;
        margin-top: 24px;
    }
    .cancel-render-btn:hover {
        background: var(--highlight-cta);
        color: #FFFFFF;
    }

    .yt-render-tag { display: inline-flex; align-items: center; justify-content: center; gap: 8px; background: var(--surface-inner); border: 1px solid var(--border-distinct); color: var(--text-primary); text-decoration: none; padding: 8px 16px; border-radius: 100px; font-weight: 700; font-size: 13px; transition: 0.2s; }
    .yt-render-tag:hover { border-color: var(--primary-yellow); }

    .queue-card {
        background: var(--surface-inner);
        border: 1px solid var(--border-distinct);
        border-radius: 18px;
        padding: 16px;
        display: flex;
        align-items: center;
        gap: 14px;
        box-sizing: border-box;
        width: 100%;
        position: relative;
        transition: all 0.25s ease;
    }
    .queue-card:hover {
        border-color: var(--primary-yellow);
    }
    .media-avatar-container { position: relative; width: 52px; height: 52px; border-radius: 12px; overflow: hidden; background: var(--surface-card); display: flex; align-items: center; justify-content: center; flex-shrink: 0; cursor: pointer; border: 1px solid var(--border-distinct); }
    .queue-info-col { flex: 1; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
    .queue-file-title { font-size: 13px; font-weight: 700; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer;}
    .circle-icon-btn { width: 40px; height: 40px; border-radius: 50%; background: var(--surface-card); display: flex; align-items: center; justify-content: center; cursor: pointer; transition: 0.2s; border: 1px solid var(--border-distinct); color: var(--text-primary); }
    .circle-icon-btn:hover { background: var(--primary-yellow); color: var(--btn-text-on-accent); border-color: var(--primary-yellow); }

    .debug-terminal { display: none; background-color: var(--surface-inner); border: 1px solid var(--highlight-cta); border-radius: 20px; padding: 16px; flex-direction: column; gap: 12px; box-sizing: border-box; width: 100%; }
    .debug-terminal h3 { margin:0; font-size:14px; color: var(--highlight-cta); display:flex; align-items:center; gap:8px; }
    .debug-log-text { background: var(--surface-card); padding: 12px; border-radius: 12px; font-family: monospace; font-size: 11px; color: var(--highlight-cta); overflow-x: auto; white-space: pre-wrap; max-height: 200px; border: 1px solid var(--border-distinct); }

    .bottom-navbar { position: fixed; bottom: 0; left: 50%; transform: translateX(-50%); width: 100%; height: 72px; background-color: var(--surface-card); border-top: 1px solid var(--border-distinct); display: flex; justify-content: space-around; align-items: center; z-index: 1000; box-sizing: border-box; padding: 0 16px; transition: opacity 0.3s ease; box-shadow: var(--card-shadow); }
    .nav-item { display: flex; flex-direction: column; align-items: center; justify-content: center; color: var(--text-muted); cursor: pointer; gap: 4px; flex: 1; height: 100%; }
    .nav-item .icon { font-size: 22px; }
    .nav-item span { font-size: 11px; font-weight: 700; }
    .nav-item.active-tab { color: var(--primary-yellow); }
    .pill-indicator-active { background-color: var(--card-glow); padding: 4px 24px; border-radius: 100px; display: flex; align-items: center; justify-content: center; margin-bottom: 2px; border: 1px solid var(--primary-yellow); }

    .interactable-node { transition: transform 0.12s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.12s ease-out !important; will-change: transform, opacity; }
    .interactable-node:active, .node-pressed { transform: scale(0.95) !important; opacity: 0.75 !important; }

    .modal-backdrop { position: fixed; top: 0; bottom: 0; left: 0; right: 0; background-color: rgba(0, 0, 0, 0.75); backdrop-filter: blur(6px); display: flex; align-items: center; justify-content: center; z-index: 2000; opacity: 0; pointer-events: none; transition: opacity 0.2s ease; }
    .modal-backdrop.show { opacity: 1; pointer-events: auto; }
    .modal-card { background-color: var(--modal-bg); border: 1px solid var(--border-distinct); border-radius: 28px; padding: 24px; width: 88%; max-width: 320px; display: flex; flex-direction: column; gap: 16px; box-shadow: var(--card-shadow); box-sizing: border-box; }
    .modal-title { font-size: 15px; font-weight: 800; margin: 0; color: var(--text-primary); text-align: center; }
    .modal-input-row { display: flex; align-items: center; justify-content: center; gap: 8px; width: 100%; box-sizing: border-box; }
    .modal-field { flex: 1; min-width: 0; background-color: var(--input-bg); border: 1px solid var(--border-distinct); border-radius: 14px; padding: 12px; color: var(--text-primary); text-align: center; font-family: var(--font); font-size: 14px; font-weight: 700; outline: none; box-sizing: border-box; }
    .modal-field:focus { border-color: var(--primary-yellow); }
    .modal-divider { color: var(--text-muted); font-weight: 800; font-size: 16px; }
    .modal-actions { display: flex; gap: 10px; justify-content: center; margin-top: 4px; width: 100%; }
    .modal-btn { padding: 12px 16px; border-radius: 100px; font-family: var(--font); font-size: 13px; font-weight: 800; border: none; cursor: pointer; flex: 1; text-align: center; box-sizing: border-box; }
    .modal-btn.save { background-color: var(--primary-yellow); color: var(--btn-text-on-accent); }
    .modal-btn.cancel { background-color: var(--surface-inner); border: 1px solid var(--border-distinct); color: var(--text-primary); }

    .empty-state {
        padding: 60px 20px;
        text-align: center;
        color: var(--text-muted);
        font-size: 15px;
        font-weight: 600;
        background: var(--surface-card);
        border: 1px dashed var(--border-distinct);
        border-radius: 24px;
        margin-top: 16px;
        position: relative;
        overflow: hidden;
    }
    .empty-state .material-icons-round {
        color: var(--text-muted);
        font-size: 48px !important;
        margin-bottom: 12px;
    }

    #mediaPreviewModal .modal-card {
        max-width: 95%;
        max-height: 88vh;
        width: 100%;
        padding: 16px;
        background: var(--surface-card);
        border: 1px solid var(--border-distinct);
        box-shadow: var(--card-shadow);
        display: flex;
        align-items: center;
        justify-content: center;
        flex-direction: column;
        gap: 12px;
        position: relative;
    }
    #fullMediaContainer {
        width: 100%;
        max-height: 75vh;
        display: flex;
        justify-content: center;
        align-items: center;
        position: relative;
        overflow: hidden;
        border-radius: 18px;
    }

    .compare-stage {
        position: relative;
        width: auto;
        height: auto;
        max-width: 100%;
        max-height: 75vh;
        overflow: hidden;
        user-select: none;
        touch-action: none;
        border-radius: 16px;
        background: #000000;
        display: inline-flex;
    }
    .compare-stage img, .compare-stage video {
        display: block;
        width: auto !important;
        height: auto !important;
        max-width: 100%;
        max-height: 75vh;
        object-fit: contain;
    }
    .compare-overlay {
        position: absolute;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        overflow: hidden;
        pointer-events: none;
        clip-path: polygon(0 0, 50% 0, 50% 100%, 0 100%);
    }
    .compare-overlay img, .compare-overlay video {
        position: absolute;
        top: 0;
        left: 0;
        width: 100% !important;
        height: 100% !important;
        object-fit: contain;
    }
    .compare-handle-line {
        position: absolute;
        top: 0;
        bottom: 0;
        left: 50%;
        width: 3px;
        background: var(--primary-yellow);
        box-shadow: 0 0 12px rgba(0, 0, 0, 0.8), 0 0 8px var(--primary-yellow);
        transform: translateX(-50%);
        pointer-events: none;
        z-index: 20;
    }
    .compare-handle-btn {
        position: absolute;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%);
        width: 36px;
        height: 36px;
        border-radius: 50%;
        background: var(--primary-yellow);
        color: #000000;
        display: flex;
        align-items: center;
        justify-content: center;
        box-shadow: 0 4px 14px rgba(0,0,0,0.6);
        border: 2px solid #FFFFFF;
        cursor: ew-resize;
        pointer-events: auto;
        z-index: 25;
    }
    .compare-label {
        position: absolute;
        top: 12px;
        padding: 4px 10px;
        border-radius: 100px;
        font-size: 10px;
        font-weight: 800;
        letter-spacing: 0.5px;
        z-index: 15;
        pointer-events: none;
        backdrop-filter: blur(4px);
    }
    .compare-label.left {
        left: 12px;
        background: rgba(0, 0, 0, 0.65);
        color: #FFFFFF;
        border: 1px solid rgba(255, 255, 255, 0.3);
    }
    .compare-label.right {
        right: 12px;
        background: rgba(255, 214, 0, 0.85);
        color: #000000;
        border: 1px solid var(--primary-yellow);
    }

    .file-status-card {
        display: flex;
        align-items: center;
        gap: 12px;
        background: var(--surface-inner);
        border: 1px solid var(--border-distinct);
        border-radius: 16px;
        padding: 10px 16px;
        margin-top: 10px;
        width: 100%;
        max-width: 340px;
        box-sizing: border-box;
        transition: all 0.3s ease;
    }

    .file-status-icon {
        width: 36px;
        height: 36px;
        border-radius: 10px;
        background: var(--card-glow);
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
        border: 1px solid var(--border-distinct);
    }
    .file-status-icon .material-icons-round {
        font-size: 20px;
        color: var(--primary-yellow);
    }
    .file-status-details {
        flex: 1;
        min-width: 0;
        display: flex;
        flex-direction: column;
        gap: 4px;
    }
    .file-status-name {
        font-size: 13px;
        font-weight: 700;
        color: var(--text-primary);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .file-status-meta {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 11px;
        font-weight: 600;
        color: var(--text-muted);
    }
    .file-status-bar {
        flex: 1;
        height: 4px;
        background: var(--surface-card);
        border: 1px solid var(--border-distinct);
        border-radius: 100px;
        overflow: hidden;
        min-width: 40px;
    }
    .file-status-fill {
        height: 100%;
        width: 0%;
        background: var(--primary-yellow);
        border-radius: 100px;
        transition: width 0.4s ease;
    }
    .upload-box > * {
      pointer-events: none;
    }

    .extreme-res-badge {
        display: none;
        align-items: center;
        gap: 6px;
        background: rgba(239, 68, 68, 0.12);
        border: 1px solid rgba(239, 68, 68, 0.35);
        color: #F87171;
        padding: 6px 14px;
        border-radius: 100px;
        font-size: 11px;
        font-weight: 700;
        margin-top: 10px;
        animation: pulse-border 2s infinite ease-in-out;
        box-sizing: border-box;
        text-align: center;
        max-width: 340px;
    }
    @keyframes pulse-border {
        0%, 100% { border-color: rgba(239, 68, 68, 0.35); }
        50% { border-color: rgba(239, 68, 68, 0.8); }
    }

    .interp-notice-badge {
        display: none;
        align-items: center;
        gap: 6px;
        background: var(--card-glow);
        border: 1px solid var(--primary-yellow);
        color: var(--primary-yellow);
        padding: 6px 14px;
        border-radius: 100px;
        font-size: 11px;
        font-weight: 700;
        margin-top: 8px;
        box-sizing: border-box;
        text-align: center;
        max-width: 360px;
        animation: pulse-border-yellow 2s infinite ease-in-out;
    }
    @keyframes pulse-border-yellow {
        0%, 100% { border-color: rgba(255, 214, 0, 0.4); }
        50% { border-color: rgba(255, 214, 0, 0.95); }
    }

    #downloadOverlay {
        position: fixed;
        bottom: 85px;
        right: 16px;
        width: calc(100% - 32px);
        max-width: 360px;
        background: var(--surface-card);
        border: 1px solid var(--border-distinct);
        border-radius: 20px;
        box-shadow: var(--card-shadow);
        z-index: 1500;
        display: none;
        flex-direction: column;
        padding: 14px;
        box-sizing: border-box;
        gap: 10px;
        max-height: 380px;
        overflow: hidden;
        animation: slideFadeIn 0.3s ease forwards;
    }
    .dl-overlay-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-bottom: 1px solid var(--border-distinct);
        padding-bottom: 8px;
    }
    .dl-overlay-title {
        font-size: 13px;
        font-weight: 800;
        color: var(--text-primary);
        display: flex;
        align-items: center;
        gap: 6px;
    }
    .dl-overlay-badge {
        background: var(--primary-yellow);
        color: var(--btn-text-on-accent);
        font-size: 10px;
        font-weight: 800;
        padding: 2px 6px;
        border-radius: 100px;
    }
    .dl-overlay-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
        overflow-y: auto;
        max-height: 280px;
        padding-right: 2px;
    }
    .dl-overlay-list::-webkit-scrollbar { width: 4px; }
    .dl-overlay-list::-webkit-scrollbar-thumb { background: var(--border-distinct); border-radius: 4px; }

    .dl-item-card {
        background: var(--surface-inner);
        border: 1px solid var(--border-distinct);
        border-radius: 14px;
        padding: 8px 10px;
        display: flex;
        flex-direction: column;
        gap: 6px;
        position: relative;
    }
    .dl-item-top {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 8px;
    }
    .dl-item-name {
        font-size: 12px;
        font-weight: 700;
        color: var(--text-primary);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        flex: 1;
    }
    .dl-item-cancel {
        color: var(--text-muted);
        cursor: pointer;
        font-size: 16px;
        transition: 0.2s;
        flex-shrink: 0;
    }
    .dl-item-cancel:hover { color: var(--highlight-cta); }
    .dl-item-bar {
        width: 100%;
        height: 4px;
        background: var(--surface-card);
        border-radius: 100px;
        overflow: hidden;
        border: 1px solid var(--border-distinct);
    }
    .dl-item-fill {
        height: 100%;
        width: 0%;
        background: var(--primary-yellow);
        border-radius: 100px;
        transition: width 0.15s linear;
    }
    .dl-item-meta {
        display: flex;
        justify-content: space-between;
        align-items: center;
        font-size: 10px;
        font-weight: 600;
        color: var(--text-muted);
    }
</style>
</head>
<body style="position:relative; min-height:100vh;">

<div class="wm-target" style="position: fixed; bottom: 85px; right: 16px; background: var(--surface-card); border: 1px solid var(--primary-yellow); padding: 6px 14px; border-radius: 100px; font-size: 11px; font-weight: 800; color: var(--primary-yellow); z-index: 500; pointer-events: none; opacity: 0.9; letter-spacing: 1px; box-shadow: var(--card-shadow);"></div>

<!-- DOWNLOAD MANAGER OVERLAY -->
<div id="downloadOverlay">
    <div class="dl-overlay-header">
        <div class="dl-overlay-title">
            <span class="material-icons-round" style="font-size: 16px; color: var(--primary-yellow);">download</span>
            Downloads
            <span class="dl-overlay-badge" id="dlActiveCount">0</span>
        </div>
    </div>
    <div class="dl-overlay-list" id="dlOverlayList"></div>
</div>

<div id="startupLoader" onclick="handleUserWakeTap()" style="cursor: pointer;">
    <svg class="md3-spinner" viewBox="0 0 50 50"><circle cx="25" cy="25" r="20"></circle></svg>
    <div class="loader-title" id="loaderTitleText">Reconnecting CrownScaler</div>
    <div class="loader-subtitle" id="loaderSubtitleText" style="margin-bottom: 8px;">Waking up environment...</div>

    <div id="loaderHintText" style="font-size: 13px; color: var(--text-primary); margin-bottom: 20px; font-weight: 700; text-align: center; opacity: 1;">
        <span id="firstRunNotice" style="color: var(--text-muted); font-weight: 600;">Please wait 1-2 minutes to setup (First run only)<br><br></span>
        <span id="reconnectInstruction" style="display: none;">Please click Play button again if it's not reconnecting.<br><br></span>
        <span id="awakePromptBadge" style="display: inline-flex; align-items: center; justify-content: center; gap: 6px; color: var(--primary-yellow); background: var(--card-glow); font-size: 12px; font-weight: 700; padding: 8px 18px; border: 1px dashed var(--primary-yellow); border-radius: 100px; margin-top: 4px; cursor: pointer; transition: all 0.2s ease;">
            <span class="material-icons-round" style="font-size: 16px;">touch_app</span>
            Tap anywhere to keep screen awake
        </span>
    </div>

    <div id="loaderPercentage" style="font-size: 38px; font-weight: 800; color: var(--primary-yellow); letter-spacing: -1px; margin-bottom: 8px; font-variant-numeric: tabular-nums;">0%</div>

    <div class="wavy-progress-bg"><div class="wavy-progress-fill"></div></div>
    <div class="loader-timer" id="loaderTimer" style="margin-top: 6px;">00:00</div>
</div>

<div class="studio-canvas" id="mainApp" style="display:none;">
    <div class="app-header">
        <div style="display:flex; align-items:center;">
            <a href="https://youtube.com/@crown08?si=3Jhi8pxZt82YQEtw" target="_blank" class="brand-link-wrapper interactable-node">
                <h1 class="app-title-main">Crown<span>Scaler</span></h1>
            </a>
        </div>

        <div style="display:flex; gap:12px; align-items:center;">
            <div style="display:flex; gap:6px;">
                <a href="https://youtube.com/@crown08?si=3Jhi8pxZt82YQEtw" target="_blank" class="social-symbol yt interactable-node"><i class="fa-brands fa-youtube"></i></a>
                <a href="https://discord.gg/87nACnWeVV" target="_blank" class="social-symbol dc interactable-node"><i class="fa-brands fa-discord"></i></a>
                <a href="https://crown-project.pages.dev/" target="_blank" class="social-symbol wb interactable-node"><i class="fa-solid fa-globe"></i></a>
            </div>
            <span class="material-icons-round icon-btn interactable-node" id="themeToggleBtn" onclick="cycleTheme()" title="Toggle Theme">light_mode</span>
            <span class="material-icons-round icon-btn interactable-node" id="bellIcon" onclick="toggleNotifyModal()">notifications</span>
        </div>

        <div class="notify-dropdown" id="notifyDropdown">
            <h3 style="margin:0; font-size:14px; margin-bottom:4px; color: var(--text-primary);">Notification Settings</h3>
            <div class="switch-row">
                <span style="font-size:13px; font-weight:600; color: var(--text-primary);">Play sound on complete</span>
                <label class="switch-widget"><input type="checkbox" id="soundToggle" checked><span class="switch-slider"></span></label>
            </div>
        </div>
    </div>

    <div id="view-upscale" class="view-section active-view">
        <div class="compose-card span-full">
            <div id="dropZone" class="upload-box interactable-node" onclick="document.getElementById('nativeFileInput').click()">
                <div class="upload-circle"><span class="material-icons-round icon">cloud_upload</span></div>
                <div class="upload-title">Select Videos or Images</div>
                <div class="upload-subtitle">Tap or drag files here to upload (All Resolutions Supported)</div>
                <input type="file" accept="video/*, image/*" multiple id="nativeFileInput" style="display: none;" onchange="handleMultipleFiles(this)">
            </div>

            <!-- DIRECT URL & GOOGLE DRIVE MEDIA IMPORTER (Pill Styled) -->
            <div class="url-import-container">
                <input type="text" class="url-import-input" id="mediaUrlInput" placeholder="Paste direct media URL or Google Drive link...">
                <button class="url-import-btn interactable-node" id="importUrlBtn" onclick="importFromLink()">
                    <span class="material-icons-round" style="font-size:16px;">link</span>
                    <span>Import Link</span>
                </button>
            </div>

            <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 16px;">
                <h3 class="component-title" style="color: var(--text-muted);">Queue</h3>
                <span class="material-icons-round icon-btn interactable-node" style="color:var(--text-muted); font-size: 20px;" onclick="triggerPythonRefresh()" title="Refresh Files">refresh</span>
            </div>
            <div id="unsupportedCard" class="compose-card" style="display:none; background: var(--surface-inner); border: 1px solid var(--highlight-cta); border-radius: 20px; padding: 14px; margin-top: 12px;">
                <h4 style="margin:0; color: var(--highlight-cta); font-size: 14px; font-weight: 700;">
                    <span class="material-icons-round icon" style="font-size: 18px; vertical-align: middle;">warning</span>
                    <span id="unsupportedCount">0</span> file(s) failed during processing
                </h4>
                <ul id="unsupportedFileList" style="list-style: none; padding: 0; margin: 8px 0 0 0; font-size: 12px; color: var(--text-muted);"></ul>
            </div>
            <div class="file-stack" id="fileStack"></div>
        </div>

        <button class="action-button-main interactable-node" id="startUpscaleBtn" onclick="triggerUpscaleStateToggle()">
            <span class="material-icons-round icon">auto_awesome</span> Start Upscale
        </button>

        <div class="dashboard-layout">
            <div class="compose-card">
                <div class="component-label-group"><span class="material-icons-round icon">psychology</span><h3 class="component-title">AI Engine Model</h3></div>
                <div class="chip-container">
                    <button class="selectable-chip interactable-node" id="m1" onclick="selectModel('m1', 'Anime Ultra')">Anime Ultra</button>
                    <button class="selectable-chip interactable-node active" id="m2" onclick="selectModel('m2', 'Anime-Pro')">Anime-Pro</button>
                    <button class="selectable-chip interactable-node" id="m3" onclick="selectModel('m3', 'Real World')">Real World</button>
                    <button class="selectable-chip interactable-node" id="m4" onclick="selectModel('m4', 'Custom')">Custom</button>
                </div>

                <div class="custom-model-drawer" id="customModelDrawer">
                    <div style="font-size:12px; font-weight:700; color:var(--primary-yellow); display:flex; align-items:center; gap:6px;">
                        <span class="material-icons-round" style="font-size:16px;">extension</span>
                        <span>Upload Model (.engine or .onnx)</span>
                    </div>

                    <div style="display:flex; gap:8px; width:100%; align-items:center;">
                        <button class="model-upload-btn interactable-node" style="flex:1;" onclick="document.getElementById('nativeModelInput').click()">
                            <span class="material-icons-round icon" style="font-size:18px; color:var(--primary-yellow);">upload_file</span>
                            <span id="customModelUploadLabel">Upload File</span>
                        </button>
                        <input type="file" id="nativeModelInput" accept=".engine,.onnx" style="display:none;" onchange="handleCustomModelUpload(this)">
                    </div>

                    <div style="display:flex; flex-direction:column; gap:4px; width:100%;">
                        <span style="font-size:11px; font-weight:600; color:var(--text-muted);">Or direct download link / Google Drive URL:</span>
                        <input type="text" class="model-input-link" id="customModelUrl" placeholder="https://.../model.engine or .onnx" oninput="STATE_CUSTOM_URL=this.value.trim()">
                    </div>
                </div>
            </div>

            <div class="compose-card">
                <div class="slider-info-row">
                    <div class="component-label-group"><span class="material-icons-round icon">tune</span><h3 class="component-title">Video Quality CRF</h3></div>
                    <div class="badge-pills" id="crfLiveNum">15</div>
                </div>
                <div class="range-container">
                    <input type="range" class="range-input interactable-node" id="optSlider" min="1" max="51" value="15" oninput="updateCrfDisplay(this.value)">
                </div>
                <div class="slider-labels"><span>Quality</span><span>Speed</span></div>
            </div>

            <!-- EXTRA TUNING SECTION -->
            <div class="compose-card span-full" style="border: 1px solid var(--primary-yellow); background: linear-gradient(180deg, var(--surface-card) 0%, var(--surface-inner) 100%);">
                <div class="component-label-group">
                    <span class="material-icons-round icon" style="color:var(--primary-yellow);">tune</span>
                    <h3 class="component-title">Extra Tuning</h3>
                </div>

                <div class="dashboard-layout" style="margin-top: 6px;">
                    <div style="display:flex; flex-direction:column; gap:6px;">
                        <div class="slider-info-row">
                            <span style="font-size:13px; font-weight:700; color:var(--text-primary);"><span class="material-icons-round icon" style="font-size:15px; vertical-align:middle; color:var(--primary-yellow);">grain</span> Recover Original Details</span>
                            <span class="badge-pills" id="recoverDetailsNum">0%</span>
                        </div>
                        <div class="range-container">
                            <input type="range" class="range-input interactable-node" id="recoverDetailsSlider" min="0" max="100" value="0" oninput="document.getElementById('recoverDetailsNum').innerText = this.value + '%'">
                        </div>
                        <div class="slider-labels"><span>Disabled</span><span>100% (High Texture)</span></div>
                    </div>

                    <div style="display:flex; flex-direction:column; gap:6px;">
                        <div class="slider-info-row">
                            <span style="font-size:13px; font-weight:700; color:var(--text-primary);"><span class="material-icons-round icon" style="font-size:15px; vertical-align:middle; color:var(--primary-yellow);">details</span> Sharpen / De-Blur</span>
                            <span class="badge-pills" id="sharpenNum">0%</span>
                        </div>
                        <div class="range-container">
                            <input type="range" class="range-input interactable-node" id="sharpenSlider" min="0" max="100" value="0" oninput="document.getElementById('sharpenNum').innerText = this.value + '%'">
                        </div>
                        <div class="slider-labels"><span>Soft</span><span>Crisp Edges</span></div>
                    </div>

                    <div style="display:flex; flex-direction:column; gap:6px;">
                        <div class="slider-info-row">
                            <span style="font-size:13px; font-weight:700; color:var(--text-primary);"><span class="material-icons-round icon" style="font-size:15px; vertical-align:middle; color:var(--primary-yellow);">filter_vintage</span> Reduce Noise / Denoise</span>
                            <span class="badge-pills" id="denoiseNum">0%</span>
                        </div>
                        <div class="range-container">
                            <input type="range" class="range-input interactable-node" id="denoiseSlider" min="0" max="100" value="0" oninput="document.getElementById('denoiseNum').innerText = this.value + '%'">
                        </div>
                        <div class="slider-labels"><span>Raw</span><span>Clean Smoothing</span></div>
                    </div>

                    <div style="display:flex; flex-direction:column; gap:6px;">
                        <div class="slider-info-row">
                            <span style="font-size:13px; font-weight:700; color:var(--text-primary);"><span class="material-icons-round icon" style="font-size:15px; vertical-align:middle; color:var(--primary-yellow);">blur_off</span> De-Halo / Anti-Ringing</span>
                            <span class="badge-pills" id="dehaloNum">0%</span>
                        </div>
                        <div class="range-container">
                            <input type="range" class="range-input interactable-node" id="dehaloSlider" min="0" max="100" value="0" oninput="document.getElementById('dehaloNum').innerText = this.value + '%'">
                        </div>
                        <div class="slider-labels"><span>Off</span><span>Max Edge Clean</span></div>
                    </div>
                </div>
            </div>

            <div class="compose-card flex-col-grid">
                <div class="component-label-group"><span class="material-icons-round icon">aspect_ratio</span><h3 class="component-title">Output Resolution</h3></div>
                <div class="flex-row-grid">
                    <button class="block-selector interactable-node active" id="r0" onclick="selectResolution('r0','UPSCALED')">UPSCALED</button>
                    <button class="block-selector interactable-node" id="r1" onclick="selectResolution('r1','1080P')">1080P</button>
                    <button class="block-selector interactable-node" id="r2" onclick="selectResolution('r2','Custom')">Custom</button>
                </div>
            </div>

            <div class="compose-card flex-col-grid">
                <div class="component-label-group"><span class="material-icons-round icon">biotech</span><h3 class="component-title">Codec Target</h3></div>
                <div class="flex-row-grid">
                    <button class="block-selector interactable-node" id="c1" onclick="selectCodec('c1','H.264')">H.264</button>
                    <button class="block-selector interactable-node active" id="c2" onclick="selectCodec('c2','H.265')">H.265</button>
                </div>
            </div>

            <div class="compose-card span-full">
                <!-- 120 FPS OPTION WITH DROPDOWN (DEAD FRAME REMOVAL & SPEED SLIDER) -->
                <div style="display:flex; flex-direction:column; gap:8px; background: var(--card-glow); padding: 12px 14px; border-radius: 20px; border: 1px solid var(--primary-yellow); margin-bottom: 6px;">
                    <div class="switch-row">
                        <div class="component-label-group">
                            <span class="material-icons-round icon" style="color:var(--primary-yellow); font-size:22px;">speed</span>
                            <div style="display:flex; flex-direction:column; gap:3px;">
                                <div style="display:flex; align-items:center; gap:8px;">
                                    <h3 class="component-title" style="margin:0;">120 FPS AI Interpolation</h3>
                                    <span style="background:var(--primary-yellow); color:#000000; font-size:9px; font-weight:800; padding:2px 7px; border-radius:6px; letter-spacing:0.5px;">RIFE v4.26</span>
                                    <span class="material-icons-round drawer-chevron-btn" id="fpsChevron" onclick="toggleFpsDrawer()" title="Expand Options">expand_more</span>
                                </div>
                                <span style="font-size:11px; color:var(--text-muted); font-weight:500;">Interpolates lower-FPS video up to 120 FPS. Automatically skips 120+ FPS clips.</span>
                            </div>
                        </div>
                        <label class="switch-widget"><input type="checkbox" id="fps120Toggle" onchange="toggleFpsDrawer(this.checked)"><span class="switch-slider"></span></label>
                    </div>

                    <!-- EXPANDABLE DROPDOWN FOR 120 FPS -->
                    <div class="fps-drawer" id="fpsDrawer">
                        <!-- REMOVE DEAD FRAMES TOGGLE & SENSITIVITY -->
                        <div style="display:flex; flex-direction:column; gap:8px;">
                            <div class="switch-row">
                                <div style="display:flex; flex-direction:column; gap:2px;">
                                    <div style="display:flex; align-items:center; gap:6px;">
                                        <span class="material-icons-round" style="font-size:16px; color:var(--primary-yellow);">filter_frames</span>
                                        <span style="font-size:13px; font-weight:700; color:var(--text-primary);">Remove Dead Frames</span>
                                    </div>
                                    <span style="font-size:10px; color:var(--text-muted);">Eliminates duplicate/frozen frames before interpolating (Ideal for 2D Anime on 2s/3s).</span>
                                </div>
                                <label class="switch-widget"><input type="checkbox" id="removeDeadToggle" onchange="toggleDeadSensitivity(this.checked)"><span class="switch-slider"></span></label>
                            </div>

                            <div id="deadThresholdContainer" style="display:none; flex-direction:column; gap:6px; margin-top:4px; padding-left:22px;">
                                <div class="slider-info-row">
                                    <span style="font-size:12px; font-weight:600; color:var(--text-muted);">Detection Sensitivity (Threshold)</span>
                                    <span class="badge-pills" id="deadThreshLive">3%</span>
                                </div>
                                <div class="range-container">
                                    <input type="range" class="range-input interactable-node" id="deadThresholdSlider" min="1" max="15" step="0.5" value="3" oninput="document.getElementById('deadThreshLive').innerText = this.value + '%'">
                                </div>
                                <div class="slider-labels"><span>Strict (1%)</span><span>Moderate (3%)</span><span>Aggressive (15%)</span></div>
                            </div>
                        </div>

                        <!-- SPEED FACTOR SLIDER -->
                        <div style="display:flex; flex-direction:column; gap:8px; margin-top: 6px; border-top: 1px solid var(--border-distinct); padding-top: 10px;">
                            <div class="slider-info-row">
                                <span style="font-size:13px; font-weight:700; color:var(--text-primary);">
                                    <span class="material-icons-round" style="font-size:16px; vertical-align:middle; color:var(--primary-yellow);">fast_forward</span>
                                    Playback Speed Multiplier
                                </span>
                                <span class="badge-pills" id="speedFactorLive">1.0x</span>
                            </div>
                            <div class="range-container">
                                <input type="range" class="range-input interactable-node" id="speedFactorSlider" min="0.25" max="2.0" step="0.05" value="1.0" oninput="document.getElementById('speedFactorLive').innerText = parseFloat(this.value).toFixed(2) + 'x'">
                            </div>
                            <div class="slider-labels"><span>0.25x (Slow-Mo)</span><span>1.0x (Normal)</span><span>2.0x (Fast)</span></div>
                        </div>
                    </div>
                </div>

                <!-- REVERSE TIME REMAP WITH POST-UPSCALE RSMB DRAWER -->
                <div style="display:flex; flex-direction:column; gap:8px; background: var(--surface-inner); border: 1px solid var(--border-distinct); border-radius: 20px; padding: 12px 14px; margin-bottom: 6px;">
                    <div class="switch-row">
                        <div class="component-label-group">
                            <span class="material-icons-round icon" style="color:var(--primary-yellow); font-size:22px;">change_circle</span>
                            <div style="display:flex; flex-direction:column; gap:3px;">
                                <div style="display:flex; align-items:center; gap:8px;">
                                    <h3 class="component-title" style="margin:0;">Reverse Time Remap</h3>
                                    <span style="background:linear-gradient(135deg, var(--primary-yellow), var(--dark-yellow)); color:#000000; font-size:9px; font-weight:800; padding:2px 7px; border-radius:6px; letter-spacing:0.5px;">Microwave FX</span>
                                </div>
                                <span style="font-size:11px; color:var(--text-muted); font-weight:500;">Applies exact cubic bezier microwave time curve with post-upscale motion blur.</span>
                            </div>
                        </div>
                        <label class="switch-widget"><input type="checkbox" id="reverseRemapToggle" onchange="toggleRemapDrawer(this.checked)"><span class="switch-slider"></span></label>
                    </div>

                    <div class="remap-drawer" id="remapDrawer">
                        <div style="display:flex; flex-direction:column; gap:8px;">
                            <div class="slider-info-row">
                                <span style="font-size:13px; font-weight:700; color:var(--text-primary);">
                                    <span class="material-icons-round icon" style="font-size:16px; vertical-align:middle; color:var(--primary-yellow);">blur_on</span>
                                    Real Time Motion Blur (Post-Upscale RSMB)
                                </span>
                                <span class="badge-pills" id="rsmbLiveVal">0%</span>
                            </div>
                            <div class="range-container">
                                <input type="range" class="range-input interactable-node" id="rsmbSlider" min="0" max="100" value="0" oninput="document.getElementById('rsmbLiveVal').innerText = this.value + '%'">
                            </div>
                            <div class="slider-labels"><span>Off (0%)</span><span>Natural (50%)</span><span>Max (100%)</span></div>
                        </div>

                        <div class="switch-row" style="margin-top: 6px; border-top: 1px solid var(--border-distinct); padding-top: 8px;">
                            <span style="font-size:12px; font-weight:700; color:var(--text-primary);">Shutter Angle</span>
                            <select id="shutterDropdown" class="custom-select">
                                <option value="180" selected>180° (Standard Film)</option>
                                <option value="270">270° (Cinematic Motion)</option>
                                <option value="360">360° (Dreamy Velocity)</option>
                            </select>
                        </div>
                    </div>
                </div>

                <div class="switch-row">
                    <div class="component-label-group"><span class="material-icons-round icon">backup</span><h3 class="component-title">Save exports to Drive</h3></div>
                    <label class="switch-widget"><input type="checkbox" id="driveToggle" onchange="handleDriveMount(this)"><span class="switch-slider"></span></label>
                </div>
                <div class="switch-row">
                    <div class="component-label-group"><span class="material-icons-round icon">download_for_offline</span><h3 class="component-title">Automatic Downloads</h3></div>
                    <label class="switch-widget"><input type="checkbox" id="downloadToggle" checked><span class="switch-slider"></span></label>
                </div>
                <div class="switch-row">
                    <div class="component-label-group"><span class="material-icons-round icon">audiotrack</span><h3 class="component-title">Keep Audio</h3></div>
                    <label class="switch-widget"><input type="checkbox" id="audioToggle" checked><span class="switch-slider"></span></label>
                </div>

                <div class="switch-row">
                    <div class="component-label-group">
                        <span class="material-icons-round icon">phone_iphone</span>
                        <div style="display:flex; flex-direction:column;">
                            <h3 class="component-title" style="margin:0;">iPhone Compatibility Fix</h3>
                            <span style="font-size:11px; color:var(--text-muted); font-weight:500;">Optimizes MP4 stream tags and faststart headers for mobile players.</span>
                        </div>
                    </div>
                    <label class="switch-widget"><input type="checkbox" id="iphoneFixToggle"><span class="switch-slider"></span></label>
                </div>

                <div class="switch-row" style="margin-top: 12px; border-top: 1px solid var(--border-distinct); padding-top: 12px;">
                    <div class="component-label-group"><span class="material-icons-round icon">speed</span><h3 class="component-title">Preset</h3></div>
                    <select id="presetDropdown" class="custom-select">
                        <option value="p1">P1 (Fastest)</option>
                        <option value="p2">P2</option>
                        <option value="p3">P3</option>
                        <option value="p4" selected>P4 (Medium)</option>
                        <option value="p5">P5</option>
                        <option value="p6">P6</option>
                        <option value="p7">P7 (Slowest / Best)</option>
                    </select>
                </div>
            </div>
        </div>
    </div>

    <div id="view-rendering" class="view-section">
        <div class="compose-card span-full modern-render-card">
            <div class="modern-glow-bg"></div>

            <div class="modern-render-header">
                <span class="material-icons-round pulse-icon">auto_awesome</span>
                <h2 class="render-title" id="renderTitle">Rendering</h2>
                <div class="render-subtitle" id="renderSubtitle">Initializing TensorRT pipeline...</div>

                <div id="currentFileInfo" class="file-status-card">
                    <div class="file-status-icon">
                        <span class="material-icons-round">folder</span>
                    </div>
                    <div class="file-status-details">
                        <div class="file-status-name" id="currentFileName">Preparing files...</div>
                        <div class="file-status-meta">
                            <span id="currentFileIndex">—</span>
                            <div class="file-status-bar">
                                <div class="file-status-fill" id="fileStatusFill"></div>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="extremeResWarning" class="extreme-res-badge">
                    <span class="material-icons-round" style="font-size: 15px;">warning_amber</span>
                    <span>Extreme resolution detected (4K+). Heavy processing in progress and it will take longer.</span>
                </div>

                <div id="interpNoticeBadge" class="interp-notice-badge">
                    <span class="material-icons-round" style="font-size: 15px;">auto_awesome_motion</span>
                    <span>Interpolation + Upscaling active (Generating extra frames — will take longer to render)</span>
                </div>

                <div id="remapNoticeBadge" class="interp-notice-badge" style="border-color: #38BDF8; color: #38BDF8;">
                    <span class="material-icons-round" style="font-size: 15px;">change_circle</span>
                    <span>Reverse Time Remap active (Microwave speed curve + Post-Upscale Motion Blur)</span>
                </div>
            </div>

            <div class="modern-progress-wrapper">
                <div class="percentage-display" id="statPct">0%</div>
                <div class="modern-bar-bg">
                    <div class="modern-bar-fill warmup-pulse" id="renderFill"></div>
                </div>
            </div>

            <div class="modern-stats-grid">
                <div class="modern-stat-box">
                    <div class="modern-stat-icon"><span class="material-icons-round">movie_creation</span></div>
                    <div class="modern-stat-info">
                        <span class="stat-label">Frames</span>
                        <span class="stat-value" id="statFrames">0 / 0</span>
                    </div>
                </div>
                <div class="modern-stat-box">
                    <div class="modern-stat-icon"><span class="material-icons-round">timer</span></div>
                    <div class="modern-stat-info">
                        <span class="stat-label">Elapsed</span>
                        <span class="stat-value" id="statElapsed">00:00</span>
                    </div>
                </div>
                <div class="modern-stat-box span-2">
                    <div class="modern-stat-icon"><span class="material-icons-round">hourglass_top</span></div>
                    <div class="modern-stat-info">
                        <span class="stat-label">Estimated Time Remaining</span>
                        <span class="stat-value" id="statEta">Calculating...</span>
                    </div>
                </div>
            </div>

            <button class="cancel-render-btn interactable-node" onclick="showCancelModal()">Cancel Processing</button>

            <div style="display:flex; flex-wrap:wrap; align-items:center; justify-content:center; gap:12px; margin-top:24px; z-index: 1;">
                <a href="https://youtube.com/@crown08?si=3Jhi8pxZt82YQEtw" target="_blank" class="yt-render-tag interactable-node">
                    <i class="fa-brands fa-youtube" style="color:#FF0000;"></i> YouTube
                </a>
                <a href="https://discord.gg/87nACnWeVV" target="_blank" class="yt-render-tag interactable-node">
                    <i class="fa-brands fa-discord" style="color:#5865F2;"></i> Discord
                </a>
                <a href="https://crown-project.pages.dev/" target="_blank" class="yt-render-tag interactable-node">
                    <i class="fa-solid fa-globe" style="color:#10B981;"></i> Website
                </a>
            </div>

            <div class="wm-target" style="position:relative; bottom:0; margin-top:20px; color:var(--text-muted); font-weight:600; z-index: 1;"></div>
        </div>

        <div class="debug-terminal" id="debugPanel">
            <h3><span class="material-icons-round">bug_report</span> Pipeline Notice</h3>
            <div class="debug-log-text" id="debugLogText"></div>
        </div>
    </div>

    <div id="view-exports" class="view-section">
        <div style="display:flex; justify-content:space-between; align-items:center; padding-right:8px; margin-bottom:8px;">
            <h2 class="app-title-sub">Exports</h2>
            <div style="display:flex; gap:8px; align-items:center;">
                <button class="action-button-main" style="padding:10px 20px; font-size:13px; width:auto;" onclick="createZipAndDownload()">
                    <span class="material-icons-round icon">archive</span> Download All as ZIP
                </button>
                <span class="material-icons-round icon-btn interactable-node" style="color:var(--text-muted);" onclick="triggerPythonRefresh()" title="Refresh Files">refresh</span>
            </div>
        </div>
        <div class="dashboard-layout" id="savedFilesContainer">
            <div class="empty-state">
                <span class="material-icons-round" style="font-size:32px; margin-bottom:8px; display:block;">inventory_2</span>No archives exported yet.
                <div class="wm-target" style="position:absolute; bottom:8px; right:8px; font-size:10px; color:var(--text-muted);"></div>
            </div>
        </div>
    </div>

    <div class="bottom-navbar">
        <div class="nav-item interactable-node active-tab" id="nav-upscale" onclick="routeToView('upscale')">
            <div class="pill-indicator-active"><span class="material-icons-round icon">auto_awesome</span></div><span>Upscale</span>
        </div>
        <div class="nav-item interactable-node" id="nav-exports" onclick="routeToView('exports')">
            <span class="material-icons-round icon">folder</span><span>Exports</span>
        </div>
    </div>
</div>

<!-- Modals -->
<div class="modal-backdrop" id="resolutionModal">
    <div class="modal-card">
        <h4 class="modal-title">Custom Output Resolution</h4>
        <div class="modal-input-row">
            <input type="number" class="modal-field interactable-node" id="customWidth" value="1080" placeholder="W">
            <span class="modal-divider">×</span>
            <input type="number" class="modal-field interactable-node" id="customHeight" value="1080" placeholder="H">
        </div>
        <div id="customResError" style="color:var(--highlight-cta); font-size:12px; font-weight:700; display:none; text-align:center;"></div>
        <div class="modal-actions">
            <button class="modal-btn cancel interactable-node" onclick="closeCustomModal()">Cancel</button>
            <button class="modal-btn save interactable-node" onclick="saveCustomResolution()">Save Scale</button>
        </div>
    </div>
</div>

<div class="modal-backdrop" id="deleteConfirmModal">
    <div class="modal-card">
        <h4 class="modal-title">Remove File?</h4>
        <p style="font-size:13px; color:var(--text-muted); text-align:center; margin:0;" id="deleteConfirmMessage">Are you sure you want to remove this file?</p>
        <div class="modal-actions">
            <button class="modal-btn cancel interactable-node" onclick="closeDeleteConfirmModal()">Cancel</button>
            <button class="modal-btn save interactable-node" style="background-color:var(--highlight-cta); color:#FFFFFF;" onclick="executeConfirmedDeletion()">Remove</button>
        </div>
    </div>
</div>

<div class="modal-backdrop" id="cancelRenderModal">
    <div class="modal-card">
        <h4 class="modal-title">Cancel Rendering?</h4>
        <p style="font-size:13px; color:var(--text-muted); text-align:center; margin:0;">Are you sure you want to abort the current process? Progress will be lost.</p>
        <div class="modal-actions">
            <button class="modal-btn cancel interactable-node" onclick="document.getElementById('cancelRenderModal').classList.remove('show')">No</button>
            <button class="modal-btn save interactable-node" style="background-color:var(--highlight-cta); color:#FFFFFF;" onclick="confirmCancelRendering()">Yes</button>
        </div>
    </div>
</div>

<div class="modal-backdrop" id="recoveryModal">
    <div class="modal-card">
        <h4 class="modal-title">Incomplete Process Detected</h4>
        <p style="font-size:13px; color:var(--text-muted); text-align:center; margin:0;">
            Connection was lost in the background. Do you want to restart the last process with your exact files and settings?
        </p>
        <div class="modal-actions" style="margin-top: 12px;">
            <button class="modal-btn cancel interactable-node" onclick="discardRecovery()">Discard</button>
            <button class="modal-btn save interactable-node" onclick="resumeRecovery()">Restart Job</button>
        </div>
    </div>
</div>

<div class="modal-backdrop" id="mediaPreviewModal" onclick="closeMediaPreview(event)">
    <div class="modal-card" style="align-items:center;">
        <div style="display:flex; justify-content:space-between; align-items:center; width:100%; margin-bottom:4px;">
            <div style="font-size:13px; font-weight:800; color:var(--text-primary);" id="previewModalTitle">Media Comparison</div>
            <span class="material-icons-round interactable-node" style="color:var(--text-primary); cursor:pointer; font-size:22px;" onclick="closeMediaPreview(event, true)">close</span>
        </div>
        <div id="fullMediaContainer"></div>
    </div>
</div>

<script>
    const wmArr = [67, 114, 111, 119, 110, 83, 99, 97, 108, 101, 114];
    function mapWatermarks() { document.querySelectorAll('.wm-target').forEach(el => el.innerText = wmArr.map(x => String.fromCharCode(x)).join('')); }
    mapWatermarks();

    function sanitizeFilename(name) {
        if (!name) return "media";
        const dotIdx = name.lastIndexOf(".");
        let base = dotIdx !== -1 ? name.substring(0, dotIdx) : name;
        const ext = dotIdx !== -1 ? name.substring(dotIdx) : "";
        base = base.replace(/[^\w\-.]/g, "_");
        base = base.replace(/_+/g, "_").replace(/^_+|_+$/g, "");
        if (!base) base = "media";
        return base + ext;
    }

    function escapeHTML(str) {
        if (!str) return '';
        return str.replace(/&/g, '&amp;')
                  .replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;')
                  .replace(/"/g, '&quot;')
                  .replace(/'/g, '&#39;')
                  .replace(/`/g, '&#96;');
    }

    function getSystemTheme() {
        return (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    }

    function applySavedTheme() {
        const saved = localStorage.getItem('cs_theme');
        const activeTheme = saved || getSystemTheme();
        document.documentElement.setAttribute('data-theme', activeTheme);

        const themeBtn = document.getElementById('themeToggleBtn');
        if (themeBtn) {
            themeBtn.innerText = (activeTheme === 'light') ? 'dark_mode' : 'light_mode';
        }
    }

    applySavedTheme();

    if (window.matchMedia) {
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
            if (!localStorage.getItem('cs_theme')) {
                const newTheme = e.matches ? 'dark' : 'light';
                document.documentElement.setAttribute('data-theme', newTheme);
                const themeBtn = document.getElementById('themeToggleBtn');
                if (themeBtn) themeBtn.innerText = (newTheme === 'light') ? 'dark_mode' : 'light_mode';
            }
        });
    }

    function cycleTheme() {
        const currentTheme = document.documentElement.getAttribute('data-theme') || getSystemTheme();
        const nextTheme = (currentTheme === 'light') ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', nextTheme);
        localStorage.setItem('cs_theme', nextTheme);
        const themeBtn = document.getElementById('themeToggleBtn');
        if (themeBtn) themeBtn.innerText = (nextTheme === 'light') ? 'dark_mode' : 'light_mode';
    }

    function toggleRemapDrawer(checked) {
        const drawer = document.getElementById('remapDrawer');
        if (drawer) {
            if (checked) {
                drawer.classList.add('expanded');
            } else {
                drawer.classList.remove('expanded');
            }
        }
    }

    function toggleFpsDrawer(checked) {
        const drawer = document.getElementById('fpsDrawer');
        const chevron = document.getElementById('fpsChevron');
        if (drawer) {
            const shouldExpand = typeof checked === 'boolean'
                ? checked
                : !drawer.classList.contains('expanded');
            drawer.classList.toggle('expanded', shouldExpand);
            if (chevron) {
                chevron.classList.toggle('rotated', shouldExpand);
            }
        }
    }

    function toggleDeadSensitivity(checked) {
        const c = document.getElementById('deadThresholdContainer');
        if (c) {
            c.style.display = checked ? 'flex' : 'none';
        }
    }

    async function importFromLink() {
        const input = document.getElementById('mediaUrlInput');
        const btn = document.getElementById('importUrlBtn');
        const url = input.value.trim();

        if (!url) {
            showToast("Please enter a direct link or Google Drive URL.");
            return;
        }

        btn.disabled = true;
        btn.innerHTML = '<span class="material-icons-round fa-spin" style="font-size:16px;">sync</span><span>Importing...</span>';

        try {
            const res = await google.colab.kernel.invokeFunction('notebook.import_url', [url], {});
            const data = res.data['application/json'];
            if (data && data.success) {
                showToast(`Imported: ${escapeHTML(data.filename)}`);
                input.value = '';
                triggerPythonRefresh();
            } else {
                showToast(`Import failed: ${data ? data.error : 'Invalid file'}`);
            }
        } catch (err) {
            showToast(`Import error: ${err.message}`);
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<span class="material-icons-round" style="font-size:16px;">link</span><span>Import Link</span>';
        }
    }

    let STATE_MODEL = "Anime-Pro";
    let STATE_CUSTOM_URL = "";
    let STATE_RES = "UPSCALED";
    let STATE_CODEC = "H.265";
    let POLL_INTERVAL = null;
    let RENDER_START_TIME = 0;
    let INFERENCE_START_TIME = null;
    let etaSmoothed = null;
    let recentFrameTimes = [];
    let IS_RENDERING_ACTIVE = false;
    let PREV_RES_ID = 'r0';
    let HAS_GPU_VERIFIED = true;

    let RECOVERY_CONFIG = null;
    let HAS_PROMPTED_RECOVERY = false;

    let screenWakeLock = null;
    let noSleepVid = null;
    let silentAudioElement = null;
    let silentOscillator = null;
    let heartbeatInterval = null;

    const DL_QUEUE = [];
    let DL_IS_PROCESSING = false;
    const DL_ACTIVE_TASKS = {};

    let PENDING_DELETE_PAYLOAD = null;

    function promptDeleteFile(b64Path, filename) {
        PENDING_DELETE_PAYLOAD = { b64Path, filename };
        const msgEl = document.getElementById('deleteConfirmMessage');
        if (msgEl) msgEl.innerText = `Remove "${filename}" from the queue?`;
        document.getElementById('deleteConfirmModal').classList.add('show');
    }

    function closeDeleteConfirmModal() {
        document.getElementById('deleteConfirmModal').classList.remove('show');
        PENDING_DELETE_PAYLOAD = null;
    }

    function executeConfirmedDeletion() {
        if (!PENDING_DELETE_PAYLOAD) return;
        const { b64Path, filename } = PENDING_DELETE_PAYLOAD;
        closeDeleteConfirmModal();
        deleteFileInstant(b64Path, filename);
    }

    function deleteFileInstant(b64Path, filename) {
        SELECTED_FILES.delete(filename);
        const idx = UPLOADED_FILES.indexOf(filename);
        if (idx > -1) UPLOADED_FILES.splice(idx, 1);

        const panel = document.querySelector(`.preview-panel[data-filename="${CSS.escape(filename)}"]`);
        if (panel) panel.remove();

        const absolutePath = decodeURIComponent(escape(atob(b64Path)));
        google.colab.kernel.invokeFunction('notebook.command', [JSON.stringify({command: "DELETE", file: absolutePath})], {})
            .then(() => {
                triggerPythonRefresh();
            })
            .catch(() => {});
    }

    function updateDownloadOverlayUI() {
        const overlay = document.getElementById('downloadOverlay');
        const list = document.getElementById('dlOverlayList');
        const badge = document.getElementById('dlActiveCount');

        const totalItems = Object.keys(DL_ACTIVE_TASKS).length + DL_QUEUE.length;
        if (badge) badge.innerText = totalItems;

        if (totalItems === 0) {
            if (overlay) overlay.style.display = 'none';
            return;
        }

        if (overlay && overlay.style.display !== 'flex') {
            overlay.style.display = 'flex';
        }

        if (!list) return;

        let html = '';
        for (const [key, task] of Object.entries(DL_ACTIVE_TASKS)) {
            const safeName = escapeHTML(task.filename);
            html += `
            <div class="dl-item-card" id="dl_card_${task.id}">
                <div class="dl-item-top">
                    <span class="dl-item-name" title="${safeName}">${safeName}</span>
                    <span class="material-icons-round dl-item-cancel" onclick="cancelDownloadTask('${task.id}')" title="Cancel Download">close</span>
                </div>
                <div class="dl-item-bar">
                    <div class="dl-item-fill" id="dl_fill_${task.id}" style="width: ${task.progress}%;"></div>
                </div>
                <div class="dl-item-meta">
                    <span id="dl_stat_${task.id}">${task.statusText || 'Downloading...'}</span>
                    <span id="dl_pct_${task.id}">${task.progress}%</span>
                </div>
            </div>`;
        }

        DL_QUEUE.forEach((task, idx) => {
            const safeName = escapeHTML(task.filename);
            html += `
            <div class="dl-item-card" style="opacity: 0.7;">
                <div class="dl-item-top">
                    <span class="dl-item-name" title="${safeName}">${safeName}</span>
                    <span class="material-icons-round dl-item-cancel" onclick="removeQueuedDownload(${idx})" title="Remove from Queue">close</span>
                </div>
                <div class="dl-item-bar">
                    <div class="dl-item-fill" style="width: 0%;"></div>
                </div>
                <div class="dl-item-meta">
                    <span>Queued (#${idx + 1})</span>
                    <span>Waiting</span>
                </div>
            </div>`;
        });

        list.innerHTML = html;
    }

    function cancelDownloadTask(taskId) {
        if (DL_ACTIVE_TASKS[taskId]) {
            DL_ACTIVE_TASKS[taskId].canceled = true;
            delete DL_ACTIVE_TASKS[taskId];
            updateDownloadOverlayUI();
            showToast("Download canceled");
        }
    }

    function removeQueuedDownload(index) {
        if (index >= 0 && index < DL_QUEUE.length) {
            const removed = DL_QUEUE.splice(index, 1);
            updateDownloadOverlayUI();
            if (removed[0]) showToast(`Removed ${removed[0].filename} from queue`);
        }
    }

    function enqueueDownload(b64Path, btnElement = null) {
        const absolutePath = decodeURIComponent(escape(atob(b64Path)));
        const filename = absolutePath.split('/').pop();

        const isAlreadyActive = Object.values(DL_ACTIVE_TASKS).some(t => t.path === absolutePath);
        if (isAlreadyActive || DL_QUEUE.some(t => t.path === absolutePath)) {
            showToast(`"${escapeHTML(filename)}" is already in download queue.`);
            return;
        }

        const task = {
            id: 'dl_' + Math.random().toString(36).substring(2, 9),
            path: absolutePath,
            filename: filename,
            b64Path: b64Path,
            btnElement: btnElement,
            canceled: false,
            progress: 0,
            statusText: 'Connecting...'
        };

        DL_QUEUE.push(task);
        updateDownloadOverlayUI();
        processDownloadQueue();
    }

    async function processDownloadQueue() {
        if (DL_IS_PROCESSING || DL_QUEUE.length === 0) return;
        DL_IS_PROCESSING = true;

        const currentTask = DL_QUEUE.shift();
        DL_ACTIVE_TASKS[currentTask.id] = currentTask;
        updateDownloadOverlayUI();

        let originalIcon = 'download';
        let iconEl = null;
        if (currentTask.btnElement) {
            iconEl = currentTask.btnElement.querySelector('.icon');
            if (iconEl) {
                originalIcon = iconEl.innerText;
                iconEl.innerText = 'sync';
                iconEl.classList.add('fa-spin');
            }
        }

        try {
            const infoRes = await google.colab.kernel.invokeFunction('notebook.get_file_info', [currentTask.path], {});
            const fileInfo = infoRes.data['application/json'];

            if (!fileInfo || !fileInfo.success) {
                throw new Error(fileInfo?.error || "Cannot inspect file");
            }

            const totalBytes = fileInfo.size;
            const mimeType = fileInfo.mime || 'video/mp4';
            const chunkSize = 1024 * 1024;
            const chunks = [];
            let offset = 0;
            const startTime = Date.now();

            while (offset < totalBytes) {
                if (currentTask.canceled) {
                    throw new Error("Download Aborted");
                }

                const lengthToRead = Math.min(chunkSize, totalBytes - offset);
                const chunkRes = await google.colab.kernel.invokeFunction('notebook.get_file_chunk', [currentTask.path, offset, lengthToRead], {});
                const chunkData = chunkRes.data['application/json'];

                if (!chunkData || !chunkData.success) {
                    throw new Error("Chunk transfer failed");
                }

                const byteChars = atob(chunkData.data);
                const byteNums = new Array(byteChars.length);
                for (let i = 0; i < byteChars.length; i++) {
                    byteNums[i] = byteChars.charCodeAt(i);
                }
                chunks.push(new Uint8Array(byteNums));

                offset += chunkData.bytes_read;
                const pct = Math.min(100, Math.round((offset / totalBytes) * 100));
                const elapsedSec = (Date.now() - startTime) / 1000;
                const speedMbps = elapsedSec > 0 ? ((offset * 8) / (1024 * 1024) / elapsedSec).toFixed(1) : 0;

                currentTask.progress = pct;
                currentTask.statusText = `${(offset / (1024 * 1024)).toFixed(1)} / ${(totalBytes / (1024 * 1024)).toFixed(1)} MB (${speedMbps} Mbps)`;

                const fillEl = document.getElementById(`dl_fill_${currentTask.id}`);
                const statEl = document.getElementById(`dl_stat_${currentTask.id}`);
                const pctEl = document.getElementById(`dl_pct_${currentTask.id}`);

                if (fillEl) fillEl.style.width = pct + '%';
                if (statEl) statEl.innerText = currentTask.statusText;
                if (pctEl) pctEl.innerText = pct + '%';
            }

            const blob = new Blob(chunks, { type: mimeType });
            const blobUrl = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = blobUrl;
            a.download = currentTask.filename;
            document.body.appendChild(a);
            a.click();

            setTimeout(() => {
                document.body.removeChild(a);
                URL.revokeObjectURL(blobUrl);
            }, 2000);

            showToast(`Downloaded: ${escapeHTML(currentTask.filename)}`);
        } catch (err) {
            if (err.message !== "Download Aborted") {
                showToast(`Failed downloading ${escapeHTML(currentTask.filename)}: ${err.message}`);
            }
        } finally {
            delete DL_ACTIVE_TASKS[currentTask.id];
            if (iconEl) {
                iconEl.classList.remove('fa-spin');
                iconEl.innerText = originalIcon;
            }
            updateDownloadOverlayUI();
            DL_IS_PROCESSING = false;
            setTimeout(processDownloadQueue, 300);
        }
    }

    let audioCtx = null;
    function startAudioWakeLock() {
        try {
            if (!audioCtx) {
                audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            }
            if (audioCtx.state === 'suspended') {
                audioCtx.resume();
            }

            if (!silentOscillator && audioCtx) {
                const osc = audioCtx.createOscillator();
                const gain = audioCtx.createGain();
                gain.gain.setValueAtTime(0.0001, audioCtx.currentTime);
                osc.type = 'sine';
                osc.frequency.setValueAtTime(40, audioCtx.currentTime);
                osc.connect(gain);
                gain.connect(audioCtx.destination);
                osc.start();
                silentOscillator = osc;
            }

            if (!silentAudioElement) {
                silentAudioElement = document.createElement('audio');
                silentAudioElement.setAttribute('loop', 'true');
                silentAudioElement.setAttribute('playsinline', 'true');
                silentAudioElement.volume = 0.01;
                silentAudioElement.src = "data:audio/wav;base64,UklGRigAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQQAAAAAAP8A/w==";
                document.body.appendChild(silentAudioElement);
                silentAudioElement.play().catch(e => {});
            } else if (silentAudioElement.paused) {
                silentAudioElement.play().catch(e => {});
            }
        } catch(e) {}
    }

    function unlockAudio() {
        if(!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        if(audioCtx.state === 'suspended') audioCtx.resume();
        startAudioWakeLock();
    }

    function playSuccessChime() {
        if(!audioCtx || !document.getElementById('soundToggle').checked) return;
        const osc = audioCtx.createOscillator();
        const gainNode = audioCtx.createGain();
        osc.connect(gainNode);
        gainNode.connect(audioCtx.destination);
        osc.type = 'sine';
        osc.frequency.setValueAtTime(523.25, audioCtx.currentTime);
        osc.frequency.setValueAtTime(659.25, audioCtx.currentTime + 0.15);
        gainNode.gain.setValueAtTime(0, audioCtx.currentTime);
        gainNode.gain.linearRampToValueAtTime(0.3, audioCtx.currentTime + 0.1);
        gainNode.gain.setValueAtTime(0.3, audioCtx.currentTime + 1.5);
        gainNode.gain.linearRampToValueAtTime(0, audioCtx.currentTime + 2.0);
        osc.start();
        osc.stop(audioCtx.currentTime + 2.0);
    }

    function playReadyChime() {
        try {
            if (!audioCtx) {
                audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            }
            if (audioCtx.state === 'suspended') {
                audioCtx.resume();
            }
            const osc = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.type = 'triangle';
            osc.frequency.setValueAtTime(440, audioCtx.currentTime);
            osc.frequency.setValueAtTime(880, audioCtx.currentTime + 0.5);
            gain.gain.setValueAtTime(0.2, audioCtx.currentTime);
            gain.gain.linearRampToValueAtTime(0, audioCtx.currentTime + 2.0);
            osc.start();
            osc.stop(audioCtx.currentTime + 2.0);
        } catch(e) {}
    }

    function handleUserWakeTap() {
        const badge = document.getElementById('awakePromptBadge');
        if (badge) badge.style.display = 'none';
        startAudioWakeLock();
        keepScreenAwake();
        startHeartbeat();
    }

    async function keepScreenAwake() {
        startAudioWakeLock();

        if ('wakeLock' in navigator) {
            try { screenWakeLock = await navigator.wakeLock.request('screen'); }
            catch (err) {}
        }

        if (!noSleepVid) {
            noSleepVid = document.createElement('video');
            noSleepVid.setAttribute('muted', 'true');
            noSleepVid.setAttribute('playsinline', 'true');
            noSleepVid.setAttribute('loop', 'true');
            noSleepVid.style.cssText = 'position:fixed; top:0; left:0; width:1px; height:1px; opacity:0; pointer-events:none; z-index:-9999;';
            document.body.appendChild(noSleepVid);

            let canvas = document.createElement('canvas');
            canvas.width = 1; canvas.height = 1;
            canvas.getContext('2d').fillRect(0, 0, 1, 1);
            try { noSleepVid.srcObject = canvas.captureStream(1); } catch(e) {}
        }

        if (noSleepVid.paused) {
            noSleepVid.play().catch(e => {});
        }
    }

    function letScreenSleep() {
        if (screenWakeLock !== null) {
            screenWakeLock.release().catch(()=>{});
            screenWakeLock = null;
        }
        if (noSleepVid) {
            noSleepVid.pause();
        }
    }

    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
            keepScreenAwake();
            startHeartbeat();
            triggerPythonRefresh();
        } else {
            startAudioWakeLock();
        }
    });

    document.addEventListener('touchstart', handleUserWakeTap, { once: true, passive: true });
    document.addEventListener('click', handleUserWakeTap, { once: true, passive: true });

    setTimeout(() => {
        startHeartbeat();
    }, 5000);

    function startHeartbeat() {
        if (heartbeatInterval) clearInterval(heartbeatInterval);
        heartbeatInterval = setInterval(() => {
            google.colab.kernel.invokeFunction('notebook.command', [JSON.stringify({command: "HEARTBEAT"})], {})
                .catch(() => {});
        }, 30000);
    }

    const SELECTED_FILES = new Set();
    const UPLOADED_FILES = [];

    let PROXY_URL = "";
    (async function initProxy() {
        try {
            PROXY_URL = await google.colab.kernel.proxyPort(8050);
            if(PROXY_URL.endsWith('/')) PROXY_URL = PROXY_URL.slice(0, -1);
        } catch(e) {}
    })();

    function showToast(message) {
        let container = document.getElementById('toastContainer');
        if (!container) {
            container = document.createElement('div');
            container.id = 'toastContainer';
            container.style.cssText = 'position: fixed; bottom: 85px; left: 50%; transform: translateX(-50%); z-index: 9999; display: flex; flex-direction: column; gap: 8px; pointer-events: none; width: 90%; max-width: 380px; align-items: center;';
            document.body.appendChild(container);
        }
        const toast = document.createElement('div');
        toast.style.cssText = 'background: var(--surface-card); border: 1px solid var(--primary-yellow); color: var(--text-primary); padding: 12px 20px; border-radius: 100px; font-size: 13px; font-weight: 700; box-shadow: var(--card-shadow); opacity: 0; transform: translateY(20px); transition: all 0.3s ease; text-align: center; max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;';
        toast.innerHTML = `<span class="material-icons-round" style="font-size:16px; vertical-align:middle; margin-right:6px; color:var(--primary-yellow);">info</span> ${message}`;
        container.appendChild(toast);

        requestAnimationFrame(() => { toast.style.opacity = '1'; toast.style.transform = 'translateY(0)'; });
        setTimeout(() => {
            toast.style.opacity = '0'; toast.style.transform = 'translateY(20px)';
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }

    function setNavbarLock(locked) {
        const nav = document.querySelector('.bottom-navbar');
        nav.style.opacity = locked ? '0.4' : '1';
        nav.style.pointerEvents = locked ? 'none' : 'auto';
        IS_RENDERING_ACTIVE = locked;
    }

    let bootSeconds = 0;
    let SETUP_INTERVAL = null;

    setTimeout(() => {
        keepScreenAwake();
        applySavedTheme();
        google.colab.kernel.invokeFunction('notebook.command', [JSON.stringify({command: "INIT_SETUP"})], {});

        SETUP_INTERVAL = setInterval(() => {
            google.colab.kernel.invokeFunction('notebook.get_state', [''], {}).then(res => {
                let state = res.data['application/json'];
                if(state) {
                    let pct = 0;

                    if (state.boot_start_time && state.is_resume === false) {
                        let nowSec = Date.now() / 1000;
                        bootSeconds = Math.max(0, Math.floor(nowSec - state.boot_start_time));
                    } else {
                        bootSeconds += 1;
                    }

                    if (state.is_resume === false) {
                        document.getElementById('loaderTitleText').innerText = "Setting Up CrownScaler";
                        document.getElementById('loaderSubtitleText').innerText = "Downloading dependencies (~1 to 2 mins on first run)...";

                        const reconInst = document.getElementById('reconnectInstruction');
                        if (reconInst) reconInst.style.display = 'none';

                        const firstRunNotice = document.getElementById('firstRunNotice');
                        if (firstRunNotice) firstRunNotice.style.display = 'inline';

                        if (bootSeconds < 80) {
                            pct = Math.floor((bootSeconds / 80) * 88);
                        } else {
                            pct = Math.min(99, 88 + Math.floor(((bootSeconds - 80) / 40) * 11));
                        }
                    } else {
                        const reconInst = document.getElementById('reconnectInstruction');
                        if (reconInst) reconInst.style.display = 'inline';

                        const firstRunNotice = document.getElementById('firstRunNotice');
                        if (firstRunNotice) firstRunNotice.style.display = 'none';

                        document.getElementById('loaderTitleText').innerText = "Reconnecting CrownScaler";
                        document.getElementById('loaderSubtitleText').innerText = "Waking up environment...";

                        pct = Math.min(99, Math.floor((bootSeconds / 3) * 100));
                    }

                    document.querySelector('.wavy-progress-fill').style.width = pct + '%';
                    const pctDisplay = document.getElementById('loaderPercentage');
                    if (pctDisplay) pctDisplay.innerText = pct + '%';

                    let m = Math.floor(bootSeconds / 60).toString().padStart(2, '0');
                    let s = (bootSeconds % 60).toString().padStart(2, '0');
                    document.getElementById('loaderTimer').innerText = `${m}:${s}`;

                    if (state.has_gpu === false && HAS_GPU_VERIFIED) {
                        HAS_GPU_VERIFIED = false;
                        const btn = document.getElementById('startUpscaleBtn');
                        btn.style.background = 'var(--highlight-cta)';
                        btn.style.backgroundColor = 'var(--highlight-cta)';
                        btn.style.color = '#FFFFFF';
                        btn.innerHTML = '<span class="material-icons-round icon">warning</span> Limit reached for today';
                    }

                    if(state.setup_status === 'complete') {
                        document.querySelector('.wavy-progress-fill').style.width = '100%';
                        if (pctDisplay) pctDisplay.innerText = '100%';

                        clearInterval(SETUP_INTERVAL);
                        playReadyChime();

                        setTimeout(triggerPythonRefresh, 1500);

                        document.getElementById('startupLoader').style.opacity = '0';
                        setTimeout(() => {
                            document.getElementById('startupLoader').style.display = 'none';
                            document.getElementById('mainApp').style.display = 'flex';
                            renderSavedFiles(state.saved_files);
                            if(state.input_files && state.input_files.length > 0) renderInputFiles(state.input_files);
                            const iphoneToggle = document.getElementById('iphoneFixToggle');
                            if (iphoneToggle) {
                                iphoneToggle.checked = detectIOS();
                            }

                            if (state.status === 'running') {
                                setNavbarLock(true);
                                routeToView('rendering');
                                startPolling();
                            }
                            else if (state.recovery_config && !HAS_PROMPTED_RECOVERY) {
                                RECOVERY_CONFIG = state.recovery_config;
                                HAS_PROMPTED_RECOVERY = true;
                                document.getElementById('recoveryModal').classList.add('show');
                            }
                        }, 400);
                    }
                }
            });
        }, 500);
    }, 150);

    function detectIOS() {
        const ua = navigator.userAgent || navigator.vendor || window.opera;
        const isIOSClassic = /iPad|iPhone|iPod/.test(ua) && !window.MSStream;
        const isIPadOS13Plus = navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1;
        return isIOSClassic || isIPadOS13Plus;
    }

    function toggleNotifyModal() { document.getElementById('notifyDropdown').classList.toggle('show'); }

    document.addEventListener('click', function(e) {
        const dropdown = document.getElementById('notifyDropdown');
        const bell = document.getElementById('bellIcon');
        if (!dropdown || !bell) return;
        if (!dropdown.contains(e.target) && e.target !== bell && !bell.contains(e.target)) {
            dropdown.classList.remove('show');
        }
    });

    function triggerPythonRefresh() {
        google.colab.kernel.invokeFunction('notebook.command', [JSON.stringify({command: "REFRESH"})], {}).then(() => {
            google.colab.kernel.invokeFunction('notebook.get_state', [''], {}).then(res => {
                let state = res.data['application/json'];
                renderSavedFiles(state.saved_files);
                if(state.input_files) renderInputFiles(state.input_files);
            });
        });
    }

    function routeToView(viewId) {
        if (IS_RENDERING_ACTIVE && viewId !== 'rendering') return;
        document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active-view'));
        document.querySelectorAll('.nav-item').forEach(el => {
            el.classList.remove('active-tab');
            const icon = el.querySelector('.icon'); el.innerHTML = ''; el.appendChild(icon);
            const label = document.createElement('span'); label.innerText = el.id === 'nav-upscale' ? 'Upscale' : 'Exports';
            el.appendChild(label);
        });
        document.getElementById('view-' + viewId).classList.add('active-view');

        if(viewId !== 'rendering') {
            const activeNav = document.getElementById('nav-' + viewId); activeNav.classList.add('active-tab');
            const activeIcon = activeNav.querySelector('.icon').cloneNode(true); activeNav.innerHTML = '';
            const indicator = document.createElement('div'); indicator.className = 'pill-indicator-active'; indicator.appendChild(activeIcon); activeNav.appendChild(indicator);
            const activeLabel = document.createElement('span'); activeLabel.innerText = viewId === 'upscale' ? 'Upscale' : 'Exports'; activeNav.appendChild(activeLabel);
        }
        if(viewId === 'exports') triggerPythonRefresh();
    }

    function toggleFileSelection(filename, isChecked) {
        if(isChecked) SELECTED_FILES.add(filename);
        else SELECTED_FILES.delete(filename);
    }

    function renderSavedFiles(filesArr) {
        const container = document.getElementById('savedFilesContainer');
        if(!filesArr || filesArr.length === 0) {
            container.innerHTML = `
            <div class="empty-state">
                <span class="material-icons-round" style="font-size:32px; margin-bottom:8px; display:block;">inventory_2</span>No archives exported yet.
                <div class="wm-target" style="position:absolute; bottom:8px; right:8px; font-size:10px; color:var(--text-muted);"></div>
            </div>`;
            mapWatermarks();
            return;
        }
        container.innerHTML = '';
        filesArr.forEach((file) => {
            const isVideo = file.name.match(/\.(mp4|avi|mov|mkv)$/i) ? 'true' : 'false';
            const mediaTag = file.thumb
                ? `<img src="${file.thumb}" />`
                : `<div style="width:100%; height:100%; display:flex; align-items:center; justify-content:center; background:var(--surface-inner); color:var(--text-primary); pointer-events:none;"><span class="material-icons-round">movie</span></div>`;

            const b64Path = btoa(unescape(encodeURIComponent(file.path)));
            const b64Orig = file.original_path ? btoa(unescape(encodeURIComponent(file.original_path))) : "";
            const safeName = escapeHTML(file.name);

            const driveBtnHtml = file.saved
                ? `<div class="circle-icon-btn" style="border-color:rgba(16, 185, 129, 0.3); background-color:rgba(16, 185, 129, 0.1); cursor:default;" data-saved="true"><span class="material-icons-round icon" style="color:#10B981;">check_circle</span></div>`
                : `<div class="circle-icon-btn" onclick="triggerDriveSave('${b64Path}', this)" title="Save to Drive"><span class="material-icons-round icon">add_to_drive</span></div>`;

            const html = `
            <div class="queue-card interactable-node">
                <div class="media-avatar-container" onclick="openMediaPreviewUrl('${b64Path}', ${isVideo}, '${b64Orig}')">${mediaTag}</div>
                <div class="queue-info-col">
                    <div class="queue-file-title" onclick="openMediaPreviewUrl('${b64Path}', ${isVideo}, '${b64Orig}')">${safeName}</div>
                    <div class="meta-sub-metrics">${file.size} • ${file.res}</div>
                </div>
                <div style="display:flex; gap:8px; margin-left:auto; flex-shrink:0;">
                    ${driveBtnHtml}
                    <div class="circle-icon-btn" onclick="enqueueDownload('${b64Path}', this)" title="Download"><span class="material-icons-round icon">download</span></div>
                </div>
            </div>`;
            container.insertAdjacentHTML('beforeend', html);
        });
    }

    function renderInputFiles(filesArr) {
        const stack = document.getElementById('fileStack');
        const activeNames = filesArr.map(f => f.name);

        Array.from(stack.children).forEach(child => {
            if(child.id && child.id.startsWith("uploading_")) {
                if(child.dataset.finished === "true" && activeNames.includes(child.dataset.filename)) child.remove();
            } else {
                const fname = child.dataset.filename;
                if(fname && !activeNames.includes(fname)) child.remove();
            }
        });

        filesArr.forEach((fileObj) => {
            const safeNameStr = escapeHTML(fileObj.name);
            if(stack.querySelector(`.preview-panel[data-filename="${CSS.escape(fileObj.name)}"]:not([id^="uploading_"])`)) return;

            if(!UPLOADED_FILES.includes(fileObj.name)) {
                UPLOADED_FILES.push(fileObj.name);
                SELECTED_FILES.add(fileObj.name);
            }

            const isVideo = fileObj.name.match(/\.(mp4|avi|mov|mkv)$/i) ? 'true' : 'false';
            const mediaTag = fileObj.thumb
                ? `<img src="${fileObj.thumb}" />`
                : `<div style="width:100%; height:100%; display:flex; align-items:center; justify-content:center; background:var(--surface-inner); color:var(--text-primary); pointer-events:none;"><span class="material-icons-round">movie</span></div>`;

            const isChecked = SELECTED_FILES.has(fileObj.name) ? "checked" : "";
            const b64Path = btoa(unescape(encodeURIComponent(fileObj.path)));

            const html = `
            <div class="preview-panel" data-filename="${safeNameStr}">
                <div class="file-checkbox-wrapper"><input type="checkbox" class="file-cb file-checkbox" value="${safeNameStr}" ${isChecked} onchange="toggleFileSelection('${safeNameStr}', this.checked)"></div>
                <div class="preview-media-container interactable-node" onclick="openMediaPreviewUrl('${b64Path}', ${isVideo})">${mediaTag}</div>
                <div class="preview-details">
                    <div class="preview-filename" onclick="openMediaPreviewUrl('${b64Path}', ${isVideo})">${safeNameStr}</div>
                    <div class="meta-sub-metrics">${fileObj.size} • ${fileObj.res}</div>
                </div>
                <span class="material-icons-round cancel-upload-btn" onclick="promptDeleteFile('${b64Path}', '${safeNameStr}')">close</span>
            </div>`;
            stack.insertAdjacentHTML('beforeend', html);
        });
    }

    window.cancelActiveUpload = function(fileId) {
        const panel = document.getElementById(fileId);
        if (panel) {
            panel.dataset.canceled = "true";
            const speedEl = document.getElementById(`progSpeed_${fileId}`);
            if (speedEl) speedEl.innerText = "Canceling...";
        }
    };

    function handleMultipleFiles(input) {
        if (!input.files || input.files.length === 0) return;
        let filesArr = Array.from(input.files);
        let currentIndex = 0;

        const stack = document.getElementById('fileStack');
        const timestamp = Date.now();
        const uiIds = filesArr.map((_, i) => `uploading_pending_${timestamp}_${i}`);

        filesArr.forEach((file, index) => {
            if(UPLOADED_FILES.includes(file.name)) return;

            const safeNameStr = escapeHTML(file.name);
            const fileId = uiIds[index];

            const sizeStr = file.size > 1073741824
                ? (file.size / 1073741824).toFixed(2) + " GB"
                : (file.size / 1048576).toFixed(2) + " MB";

            const uploadingHtml = `
            <div class="preview-panel" id="${fileId}" data-canceled="false" style="display: flex; flex-direction: column; align-items: stretch; gap: 8px; position: relative;">
                <div style="display:flex; align-items:center; gap: 12px; width: 100%; padding-right: 38px; box-sizing: border-box;">
                    <div class="preview-media-container" style="background: transparent; border: none;">
                        <svg class="md3-spinner" style="width:28px;height:28px;margin-bottom:0;" viewBox="0 0 50 50"><circle cx="25" cy="25" r="20"></circle></svg>
                    </div>
                    <div class="preview-details" style="flex:1; display:flex; flex-direction:column; justify-content:center; min-width: 0;">
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px; gap:8px;">
                            <div class="preview-filename" style="margin-bottom:0; flex:1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${safeNameStr}</div>
                            <div class="meta-sub-metrics" style="margin-bottom:0; flex-shrink:0;">${sizeStr}</div>
                        </div>
                        <div style="width: 100%; height: 4px; background: var(--surface-card); border-radius: 4px; overflow: hidden; border: 1px solid var(--border-distinct);">
                            <div id="progBar_${fileId}" style="width: 0%; height: 100%; background: var(--surface-inner); transition: width 0.1s linear;"></div>
                        </div>
                        <div style="display:flex; justify-content:space-between; font-size:10px; color:var(--text-muted); margin-top:6px; font-weight:700;">
                            <span id="progPct_${fileId}">Queued...</span>
                            <span id="progSpeed_${fileId}">Waiting</span>
                        </div>
                    </div>
                </div>
                <span class="material-icons-round cancel-upload-btn" style="top: 50%; transform: translateY(-50%);" onclick="cancelActiveUpload('${fileId}')">close</span>
            </div>`;
            stack.insertAdjacentHTML('beforeend', uploadingHtml);
        });

        function uploadNext() {
            if(currentIndex >= filesArr.length) {
                letScreenSleep();
                input.value = "";
                google.colab.kernel.invokeFunction('notebook.force_sync', [''], {})
                    .then(() => triggerPythonRefresh())
                    .catch(() => triggerPythonRefresh());
                return;
            }

            let file = filesArr[currentIndex];
            const fileId = uiIds[currentIndex];
            const panel = document.getElementById(fileId);

            if (panel && panel.dataset.canceled === "true") {
                panel.remove();
                currentIndex++;
                return uploadNext();
            }

            if(UPLOADED_FILES.includes(file.name)) {
                if (panel) panel.remove();
                showToast(`"${escapeHTML(file.name)}" is already uploaded.`);
                currentIndex++;
                return uploadNext();
            }

            if(panel) {
                const pBar = document.getElementById(`progBar_${fileId}`);
                const pPct = document.getElementById(`progPct_${fileId}`);
                if(pBar) pBar.style.background = 'var(--primary-yellow)';
                if(pPct) pPct.innerText = '0%';
            }

            const chunkSize = 1024 * 512;
            let offset = 0;
            let startTime = Date.now();

            function pushChunk() {
                if (panel && panel.dataset.canceled === "true") {
                    panel.remove();
                    const idx = UPLOADED_FILES.indexOf(file.name);
                    if (idx > -1) UPLOADED_FILES.splice(idx, 1);
                    SELECTED_FILES.delete(file.name);

                    const cleanTargetName = sanitizeFilename(file.name);
                    const fullPath = "/content/.sys_in_cache_0x9A/." + cleanTargetName + ".part";
                    google.colab.kernel.invokeFunction('notebook.command', [JSON.stringify({command: "DELETE", file: fullPath})], {}).catch(e=>console.log(e));

                    currentIndex++;
                    return uploadNext();
                }

                const slice = file.slice(offset, offset + chunkSize);
                const reader = new FileReader();
                reader.onload = (e) => {
                    const b64 = e.target.result.split(',')[1];
                    const isLastChunk = (offset + slice.size >= file.size);
                    google.colab.kernel.invokeFunction('notebook.upload_chunk', [file.name, b64, (offset===0), isLastChunk], {})
                    .then(() => {
                        offset += slice.size;
                        let pct = Math.min(100, Math.round((offset / file.size) * 100));
                        let elapsedSec = (Date.now() - startTime) / 1000;
                        let speedMbps = elapsedSec > 0 ? ((offset * 8) / (1024 * 1024) / elapsedSec).toFixed(1) : 0;

                        let pBar = document.getElementById(`progBar_${fileId}`);
                        let pPct = document.getElementById(`progPct_${fileId}`);
                        let pSpeed = document.getElementById(`progSpeed_${fileId}`);

                        if(pBar) pBar.style.width = pct + '%';
                        if(pPct) pPct.innerText = pct + '%';
                        if(pSpeed) pSpeed.innerText = speedMbps + ' Mbps';

                        if (offset < file.size) {
                            pushChunk();
                        } else {
                            if(pPct) pPct.innerText = "Finalizing...";
                            if(pSpeed) pSpeed.innerText = "Please wait";
                            if(pBar) pBar.style.backgroundColor = '#10B981';

                            if(panel) {
                                panel.dataset.finished = "true";
                                panel.dataset.filename = file.name;
                            }

                            triggerPythonRefresh();
                            currentIndex++;
                            uploadNext();
                        }
                    })
                    .catch(err => {
                        console.error("Chunk upload failed", err);
                        const pPct = document.getElementById(`progPct_${fileId}`);
                        const pBar = document.getElementById(`progBar_${fileId}`);
                        if(pPct) pPct.innerText = "Upload Error!";
                        if(pBar) pBar.style.backgroundColor = 'var(--highlight-cta)';
                        currentIndex++;
                        uploadNext();
                    });
                };
                reader.readAsDataURL(slice);
            }
            pushChunk();
        }

        keepScreenAwake();
        uploadNext();
    }

    function handleCustomModelUpload(input) {
        if (!input.files || input.files.length === 0) return;
        const file = input.files[0];
        const label = document.getElementById('customModelUploadLabel');
        if (label) label.innerText = `Uploading ${file.name}...`;

        const chunkSize = 1024 * 1024;
        let offset = 0;

        function pushModelChunk() {
            const slice = file.slice(offset, offset + chunkSize);
            const reader = new FileReader();
            reader.onload = (e) => {
                const b64 = e.target.result.split(',')[1];
                const isLastChunk = (offset + slice.size >= file.size);
                google.colab.kernel.invokeFunction('notebook.upload_model_chunk', [file.name, b64, (offset === 0), isLastChunk], {})
                    .then(() => {
                        offset += slice.size;
                        const pct = Math.min(100, Math.round((offset / file.size) * 100));
                        if (label) label.innerText = `Uploading ${pct}%`;

                        if (offset < file.size) {
                            pushModelChunk();
                        } else {
                            if (label) label.innerText = `Loaded: ${file.name}`;
                            showToast(`Custom model "${escapeHTML(file.name)}" ready for upscale.`);
                        }
                    })
                    .catch(err => {
                        if (label) label.innerText = "Upload Failed";
                        showToast(`Failed uploading custom model: ${err.message}`);
                    });
            };
            reader.readAsDataURL(slice);
        }

        pushModelChunk();
    }

    function triggerDriveSave(b64Path, btnElement) {
        if(btnElement.dataset.saved === "true") return;
        const absolutePath = decodeURIComponent(escape(atob(b64Path)));
        const icon = btnElement.querySelector('.icon');
        icon.innerText = 'sync'; icon.classList.add('fa-spin');

        google.colab.kernel.invokeFunction('notebook.command', [JSON.stringify({command: "SAVE_TO_DRIVE", file: absolutePath})], {}).then(res => {
            icon.classList.remove('fa-spin');
            if (res.data['application/json']?.success) {
                icon.innerText = 'check_circle';
                icon.style.color = '#10B981';
                btnElement.style.borderColor = 'rgba(16, 185, 129, 0.3)';
                btnElement.style.backgroundColor = 'rgba(16, 185, 129, 0.1)';
                btnElement.dataset.saved = "true";
                btnElement.style.cursor = 'default';
            }
            else { icon.innerText = 'error'; setTimeout(() => icon.innerText = 'add_to_drive', 2000); }
        }).catch(() => { icon.classList.remove('fa-spin'); icon.innerText = 'error'; setTimeout(() => icon.innerText = 'add_to_drive', 2000); });
    }

    function createZipAndDownload() {
        showToast("Creating ZIP archive...");
        google.colab.kernel.invokeFunction('notebook.command', [JSON.stringify({command: "CREATE_ZIP"})], {}).then(res => {
            const result = res.data['application/json'];
            if (result && result.success) {
                const zipPath = result.zip_path;
                const b64 = btoa(unescape(encodeURIComponent(zipPath)));
                enqueueDownload(b64);
                triggerPythonRefresh();
            } else {
                showToast('Failed to create ZIP: ' + (result?.error || 'unknown error'));
            }
        }).catch(err => {
            showToast('Error creating ZIP');
        });
    }

    function initMobileAnimationListeners() {
        document.querySelectorAll('.interactable-node, .selectable-chip, .block-selector, .modal-btn').forEach(node => {
            if(node.dataset.hasTouchBound === 'true') return;
            node.dataset.hasTouchBound = 'true';
            node.addEventListener('touchstart', () => node.classList.add('node-pressed'), { passive: true });
            node.addEventListener('touchend', () => setTimeout(() => node.classList.remove('node-pressed'), 100), { passive: true });
            node.addEventListener('touchcancel', () => node.classList.remove('node-pressed'), { passive: true });
            node.addEventListener('mousedown', () => node.classList.add('node-pressed'));
            node.addEventListener('mouseup', () => setTimeout(() => node.classList.remove('node-pressed'), 100));
            node.addEventListener('mouseleave', () => node.classList.remove('node-pressed'));
        });
    }

    function updateCrfDisplay(val) { document.getElementById('crfLiveNum').innerText = val; }

    let isDraggingSlider = false;

    function openMediaPreviewUrl(b64Path, isVideo, b64OrigPath = "") {
        if (!PROXY_URL) {
            showToast("Preview proxy is warming up. Please try again in a few seconds.");
            return;
        }

        const absolutePath = decodeURIComponent(escape(atob(b64Path)));
        const origAbsolutePath = b64OrigPath ? decodeURIComponent(escape(atob(b64OrigPath))) : "";
        const filename = absolutePath.split('/').pop();

        const container = document.getElementById('fullMediaContainer');
        const modalTitle = document.getElementById('previewModalTitle');
        if (modalTitle) modalTitle.innerText = origAbsolutePath ? `Comparison: ${filename}` : filename;

        document.getElementById('mediaPreviewModal').classList.add('show');

        const streamUrl = PROXY_URL + absolutePath;
        const origStreamUrl = origAbsolutePath ? (PROXY_URL + origAbsolutePath) : "";

        if (origAbsolutePath) {
            if (isVideo) {
                container.innerHTML = `
                <div class="compare-stage" id="compareStage">
                    <span class="compare-label left">Original</span>
                    <span class="compare-label right">CrownScaled</span>

                    <video id="cmpUpscaledVid" src="${streamUrl}" playsinline loop autoplay muted></video>

                    <div class="compare-overlay" id="cmpOverlay">
                        <video id="cmpOrigVid" src="${origStreamUrl}" playsinline loop autoplay muted></video>
                    </div>

                    <div class="compare-handle-line" id="cmpHandleLine">
                        <div class="compare-handle-btn"><span class="material-icons-round" style="font-size: 18px;">code</span></div>
                    </div>
                </div>`;
                bindVideoSync();
            } else {
                container.innerHTML = `
                <div class="compare-stage" id="compareStage">
                    <span class="compare-label left">Original</span>
                    <span class="compare-label right">CrownScaled</span>

                    <img id="cmpUpscaledImg" src="${streamUrl}" />

                    <div class="compare-overlay" id="cmpOverlay">
                        <img id="cmpOrigImg" src="${origStreamUrl}" />
                    </div>

                    <div class="compare-handle-line" id="cmpHandleLine">
                        <div class="compare-handle-btn"><span class="material-icons-round" style="font-size: 18px;">code</span></div>
                    </div>
                </div>`;
            }
            bindComparisonSlider();
        } else {
            if (isVideo) {
                container.innerHTML = `
                <div style="position:relative; width:100%; display:flex; flex-direction:column; align-items:center; justify-content:center;">
                    <video id="modalVideoPlayer" src="${streamUrl}" controls autoplay loop playsinline style="width:auto !important; height:auto !important; max-width:100%; max-height:70vh; border-radius:16px; outline:none; background:#000; box-shadow: var(--card-shadow); object-fit:contain;"></video>
                    <div class="wm-target" style="position:absolute; top:16px; left:16px; background:var(--surface-card); padding:4px 8px; border-radius:4px; font-size:10px; font-weight:bold; color:var(--text-primary); z-index:10; opacity:0.8; letter-spacing:1px;"></div>
                </div>`;
            } else {
                container.innerHTML = `
                <div style="position:relative;">
                    <img src="${streamUrl}" style="width:auto !important; height:auto !important; max-width:100%; max-height:70vh; border-radius:16px; object-fit:contain; box-shadow: var(--card-shadow);"/>
                    <div class="wm-target" style="position:absolute; top:16px; left:16px; background:var(--surface-card); padding:4px 8px; border-radius:4px; font-size:10px; font-weight:bold; color:var(--text-primary); z-index:10; opacity:0.8; letter-spacing:1px;"></div>
                </div>`;
            }
        }
        mapWatermarks();
    }

    function bindVideoSync() {
        const vUp = document.getElementById('cmpUpscaledVid');
        const vOrig = document.getElementById('cmpOrigVid');
        if (!vUp || !vOrig) return;

        vUp.addEventListener('play', () => vOrig.play().catch(()=>{}));
        vUp.addEventListener('pause', () => vOrig.pause());
        vUp.addEventListener('seeked', () => { vOrig.currentTime = vUp.currentTime; });

        setInterval(() => {
            if (vUp && vOrig && !vUp.paused) {
                if (Math.abs(vUp.currentTime - vOrig.currentTime) > 0.08) {
                    vOrig.currentTime = vUp.currentTime;
                }
            }
        }, 500);
    }

    function bindComparisonSlider() {
        const stage = document.getElementById('compareStage');
        const overlay = document.getElementById('cmpOverlay');
        const handleLine = document.getElementById('cmpHandleLine');
        if (!stage || !overlay || !handleLine) return;

        function updateSlider(clientX) {
            const rect = stage.getBoundingClientRect();
            let x = clientX - rect.left;
            x = Math.max(0, Math.min(x, rect.width));
            const pct = (x / rect.width) * 100;

            overlay.style.clipPath = `polygon(0 0, ${pct}% 0, ${pct}% 100%, 0 100%)`;
            handleLine.style.left = `${pct}%`;
        }

        function onPointerDown(e) {
            isDraggingSlider = true;
            updateSlider(e.clientX || (e.touches && e.touches[0].clientX));
        }

        function onPointerMove(e) {
            if (!isDraggingSlider) return;
            updateSlider(e.clientX || (e.touches && e.touches[0].clientX));
        }

        function onPointerUp() {
            isDraggingSlider = false;
        }

        stage.addEventListener('mousedown', onPointerDown);
        window.addEventListener('mousemove', onPointerMove);
        window.addEventListener('mouseup', onPointerUp);

        stage.addEventListener('touchstart', onPointerDown, { passive: true });
        window.addEventListener('touchmove', onPointerMove, { passive: true });
        window.addEventListener('touchend', onPointerUp, { passive: true });
    }

    function closeMediaPreview(e, force = false) {
        if(force || e.target.id === 'mediaPreviewModal') {
            document.getElementById('mediaPreviewModal').classList.remove('show');
            const container = document.getElementById('fullMediaContainer');
            const vids = container.querySelectorAll('video');
            vids.forEach(v => { v.pause(); v.src = ""; });
            container.innerHTML = '';
        }
    }

    function handleDriveMount(checkbox) {
        if (checkbox.checked) {
            google.colab.kernel.invokeFunction('notebook.command', [JSON.stringify({command: "MOUNT_DRIVE"})], {}).then(res => {
                if (res.data['application/json']?.success === false) checkbox.checked = false;
            }).catch(() => checkbox.checked = false);
        }
    }

    function selectModel(id, val) {
        ['m1', 'm2', 'm3', 'm4'].forEach(i => document.getElementById(i)?.classList.remove('active'));
        document.getElementById(id).classList.add('active');
        STATE_MODEL = val;

        const drawer = document.getElementById('customModelDrawer');
        if (drawer) {
            if (val === 'Custom') {
                drawer.classList.add('expanded');
            } else {
                drawer.classList.remove('expanded');
            }
        }
    }

    function selectResolution(id, val) {
        if (val === 'Custom') { document.getElementById('resolutionModal').classList.add('show');
        } else {
            ['r0', 'r1', 'r2'].forEach(i => document.getElementById(i)?.classList.remove('active'));
            document.getElementById(id).classList.add('active');
            STATE_RES = val; PREV_RES_ID = id; document.getElementById('r2').innerText = 'Custom';
        }
    }

    function closeCustomModal() {
        document.getElementById('resolutionModal').classList.remove('show');
        document.getElementById('customResError').style.display = 'none';
        ['r0', 'r1', 'r2'].forEach(i => document.getElementById(i)?.classList.remove('active'));
        if (document.getElementById(PREV_RES_ID)) document.getElementById(PREV_RES_ID).classList.add('active');
    }

    function saveCustomResolution() {
        const wVal = document.getElementById('customWidth').value;
        const hVal = document.getElementById('customHeight').value;
        const w = parseInt(wVal, 10);
        const h = parseInt(hVal, 10);
        const errorDiv = document.getElementById('customResError');

        if (isNaN(w) || isNaN(h) || w <= 0 || h <= 0) {
            errorDiv.innerText = "Width and height must be positive numbers.";
            errorDiv.style.display = 'block';
            return;
        }
        if (w > 8192 || h > 8192) {
            errorDiv.innerText = "Maximum dimension is 8192 per side.";
            errorDiv.style.display = 'block';
            return;
        }
        errorDiv.style.display = 'none';

        PREV_RES_ID = 'r2';
        ['r0', 'r1', 'r2'].forEach(i => document.getElementById(i)?.classList.remove('active'));
        document.getElementById('r2').classList.add('active');
        STATE_RES = `${w} x ${h}`;
        document.getElementById('r2').innerText = STATE_RES;
        document.getElementById('resolutionModal').classList.remove('show');
    }

    function selectCodec(id, val) { ['c1', 'c2'].forEach(i => document.getElementById(i)?.classList.remove('active')); document.getElementById(id).classList.add('active'); STATE_CODEC = val; }

    function formatTime(seconds) {
        if(!isFinite(seconds) || seconds < 0) return "--:--";
        let m = Math.floor(seconds / 60).toString().padStart(2, '0');
        let s = Math.floor(seconds % 60).toString().padStart(2, '0');
        return `${m}:${s}`;
    }

    function discardRecovery() {
        document.getElementById('recoveryModal').classList.remove('show');
        google.colab.kernel.invokeFunction('notebook.command', [JSON.stringify({command: "DISCARD_RECOVERY"})], {});
    }

    function resumeRecovery() {
        document.getElementById('recoveryModal').classList.remove('show');
        if(!RECOVERY_CONFIG) return;

        STATE_MODEL = RECOVERY_CONFIG.model || STATE_MODEL;
        STATE_RES = RECOVERY_CONFIG.resolution || STATE_RES;
        STATE_CODEC = RECOVERY_CONFIG.codec || STATE_CODEC;

        unlockAudio(); setNavbarLock(true); routeToView('rendering');
        document.getElementById('debugPanel').style.display = 'none';

        const renderFill = document.getElementById('renderFill');
        renderFill.style.width = '0%';
        renderFill.classList.add('warmup-pulse');

        document.getElementById('statPct').innerText = '0%';
        document.getElementById('statFrames').innerText = '0 / 0';
        document.getElementById('statElapsed').innerText = '00:00';
        document.getElementById('statEta').innerText = 'Starting engine...';
        RENDER_START_TIME = Date.now();
        INFERENCE_START_TIME = null;
        etaSmoothed = null;
        recentFrameTimes = [];

        RECOVERY_CONFIG.command = "START";
        RECOVERY_CONFIG.is_recovery = true;

        google.colab.kernel.invokeFunction('notebook.command', [JSON.stringify(RECOVERY_CONFIG)], {});
        startPolling();
    }

    function startPolling() {
        if(POLL_INTERVAL) clearInterval(POLL_INTERVAL);
        POLL_INTERVAL = setInterval(() => {
            google.colab.kernel.invokeFunction('notebook.get_state', [''], {}).then(res => {
                let state = res.data['application/json'];
                if(state) updateUIFromState(state);
            });
        }, 1000);
    }

    function showCancelModal() {
        document.getElementById('cancelRenderModal').classList.add('show');
    }

    function confirmCancelRendering() {
        document.getElementById('cancelRenderModal').classList.remove('show');
        google.colab.kernel.invokeFunction('notebook.command', [JSON.stringify({ command: "STOP" })], {});
        if(POLL_INTERVAL) clearInterval(POLL_INTERVAL);
        letScreenSleep(); setNavbarLock(false);
        routeToView('upscale');
    }

    function updateUIFromState(state) {
        document.getElementById('renderSubtitle').innerText = state.text;

        const renderFill = document.getElementById('renderFill');
        const hasKnownTotal = state.frames_total > 0;

        if (state.frames_done > 0 && hasKnownTotal) {
            renderFill.classList.remove('warmup-pulse');
            renderFill.style.width = state.progress + '%';
            document.getElementById('statPct').innerText = state.progress + '%';
        } else if (state.frames_done > 0 && !hasKnownTotal) {
            renderFill.classList.add('warmup-pulse');
            document.getElementById('statPct').innerText = '—';
        } else {
            renderFill.classList.add('warmup-pulse');
            document.getElementById('statPct').innerText = '0%';
        }

        const renderTitleEl = document.getElementById('renderTitle');
        if (state.is_remapped && state.is_interpolating) {
            if (renderTitleEl) renderTitleEl.innerText = "Reverse Time Remap + 120 FPS + Upscaling";
        } else if (state.is_remapped) {
            if (renderTitleEl) renderTitleEl.innerText = "Reverse Time Remap + Upscaling";
        } else if (state.is_interpolating) {
            if (renderTitleEl) renderTitleEl.innerText = "Interpolation + Upscaling";
        } else if (state.current_file) {
            if (renderTitleEl) renderTitleEl.innerText = state.current_file;
        }

        const interpNotice = document.getElementById('interpNoticeBadge');
        if (interpNotice) {
            interpNotice.style.display = (state.status === 'running' && state.is_interpolating) ? 'inline-flex' : 'none';
        }

        const remapNotice = document.getElementById('remapNoticeBadge');
        if (remapNotice) {
            remapNotice.style.display = (state.status === 'running' && state.is_remapped) ? 'inline-flex' : 'none';
        }

        const fileInfoEl = document.getElementById('currentFileInfo');
        const fileNameEl = document.getElementById('currentFileName');
        const fileIndexEl = document.getElementById('currentFileIndex');
        const fileFillEl = document.getElementById('fileStatusFill');
        const extremeWarning = document.getElementById('extremeResWarning');

        if (extremeWarning) {
            extremeWarning.style.display = (state.status === 'running' && state.is_4k_plus) ? 'inline-flex' : 'none';
        }

        if (fileInfoEl && fileNameEl && fileIndexEl && fileFillEl) {
            if (state.current_file) {
                fileNameEl.innerText = state.current_file;
                fileIndexEl.innerText = state.file_index_str || 'File 1 of 1';

                const match = (state.file_index_str || '').match(/File\s+(\d+)\s+of\s+(\d+)/i);
                if (match) {
                    const current = parseInt(match[1], 10);
                    const total = parseInt(match[2], 10);
                    const pct = total > 0 ? ((current - 1) / total) * 100 : 0;
                    fileFillEl.style.width = pct + '%';
                } else {
                    fileFillEl.style.width = '0%';
                }
                fileInfoEl.style.display = 'flex';
            } else if (state.status === 'running') {
                fileNameEl.innerText = 'Preparing files...';
                fileIndexEl.innerText = '';
                fileFillEl.style.width = '0%';
                fileInfoEl.style.display = 'flex';
            } else {
                fileNameEl.innerText = 'Waiting for file...';
                fileIndexEl.innerText = '';
                fileFillEl.style.width = '0%';
                fileInfoEl.style.display = 'flex';
            }
        }

        if(state.frames_total > 0 || state.frames_done > 0) {
            if (state.frames_total > 0) {
                document.getElementById('statFrames').innerText = `${state.frames_done} / ${state.frames_total}`;
            } else {
                document.getElementById('statFrames').innerText = `${state.frames_done} frames`;
            }

            let elapsed = (Date.now() - RENDER_START_TIME) / 1000;
            document.getElementById('statElapsed').innerText = formatTime(elapsed);

            if (state.frames_done > 2) {
                if (INFERENCE_START_TIME === null) {
                    INFERENCE_START_TIME = Date.now();
                }

                const infElapsed = (Date.now() - INFERENCE_START_TIME) / 1000;
                recentFrameTimes.push({ frames: state.frames_done, time: infElapsed });

                if (recentFrameTimes.length > 10) {
                    recentFrameTimes.shift();
                }

                if (recentFrameTimes.length >= 3 && infElapsed > 1.5 && state.frames_total > 0) {
                    const first = recentFrameTimes[0];
                    const last = recentFrameTimes[recentFrameTimes.length - 1];
                    const deltaFrames = last.frames - first.frames;
                    const deltaTime = last.time - first.time;

                    if (deltaTime > 0 && deltaFrames > 0) {
                        const instantFps = deltaFrames / deltaTime;
                        const remainingFrames = state.frames_total - state.frames_done;

                        if (remainingFrames > 0) {
                            const rawEta = remainingFrames / instantFps;

                            if (etaSmoothed === null) {
                                etaSmoothed = rawEta;
                            } else {
                                etaSmoothed = (etaSmoothed * 0.85) + (rawEta * 0.15);
                            }

                            document.getElementById('statEta').innerText = formatTime(Math.round(etaSmoothed));
                        } else {
                            document.getElementById('statEta').innerText = "Finishing...";
                        }
                    }
                } else if (state.frames_total <= 0) {
                    document.getElementById('statEta').innerText = "Processing...";
                } else {
                    document.getElementById('statEta').innerText = "Estimating...";
                }
            } else {
                document.getElementById('statEta').innerText = "Warming up...";
            }
        }

        if (state.unsupported_files && Object.keys(state.unsupported_files).length > 0) {
            const card = document.getElementById('unsupportedCard');
            const count = document.getElementById('unsupportedCount');
            const list = document.getElementById('unsupportedFileList');
            if (card && count && list) {
                card.style.display = 'flex';
                count.innerText = Object.keys(state.unsupported_files).length;
                list.innerHTML = '';
                Object.entries(state.unsupported_files).forEach(([f, err]) => {
                    list.innerHTML += `<li style="padding: 2px 0;">• <strong>${escapeHTML(f)}</strong> — ${escapeHTML(err)}</li>`;
                });
            }
        }

        if(state.status === 'complete') {
            if(POLL_INTERVAL) clearInterval(POLL_INTERVAL);
            letScreenSleep(); setNavbarLock(false); playSuccessChime();

            const autoDlToggle = document.getElementById('downloadToggle');
            if(autoDlToggle && autoDlToggle.checked && state.new_completed_files && state.new_completed_files.length > 0) {
                const filesToDownload = [...state.new_completed_files];
                state.new_completed_files = [];

                filesToDownload.forEach(fname => {
                    const fullPath = `/content/.sys_out_vault_0x9B/${fname}`;
                    const b64 = btoa(unescape(encodeURIComponent(fullPath)));
                    enqueueDownload(b64);
                });
            }

            renderSavedFiles(state.saved_files);
            routeToView('exports');
        }
        else if(state.status === 'error') {
            if(POLL_INTERVAL) clearInterval(POLL_INTERVAL);
            letScreenSleep(); setNavbarLock(false);
            document.getElementById('debugPanel').style.display = 'flex';
            document.getElementById('debugLogText').innerText = state.error_log;
        }
    }

    function triggerUpscaleStateToggle() {
        if(!HAS_GPU_VERIFIED) {
            alert("Limit reached for today, change your google account to bypass it or wait hours so you can use it again.");
            return;
        }

        const activeFiles = Array.from(document.querySelectorAll('.file-cb:checked:not(:disabled)')).map(cb => cb.value);
        const filesToProcess = activeFiles.filter(f => SELECTED_FILES.has(f));

        if (filesToProcess.length === 0) {
            showToast("Please select at least one file to upscale.");
            return;
        }

        unlockAudio(); keepScreenAwake(); setNavbarLock(true); routeToView('rendering');
        document.getElementById('debugPanel').style.display = 'none';

        const renderFill = document.getElementById('renderFill');
        renderFill.style.width = '0%';
        renderFill.classList.add('warmup-pulse');

        document.getElementById('statPct').innerText = '0%';
        document.getElementById('statFrames').innerText = '0 / 0';
        document.getElementById('statElapsed').innerText = '00:00';
        document.getElementById('statEta').innerText = 'Warming up...';
        document.getElementById('renderSubtitle').innerText = 'Initializing TensorRT pipeline...';

        RENDER_START_TIME = Date.now();
        INFERENCE_START_TIME = null;
        etaSmoothed = null;
        recentFrameTimes = [];

        const payload = {
            files: filesToProcess,
            model: STATE_MODEL,
            customModelUrl: STATE_CUSTOM_URL,
            resolution: STATE_RES,
            codec: STATE_CODEC,
            crfValue: document.getElementById('optSlider').value,
            preset: document.getElementById('presetDropdown').value,
            recoverDetails: document.getElementById('recoverDetailsSlider').value,
            sharpenVal: document.getElementById('sharpenSlider').value,
            denoiseVal: document.getElementById('denoiseSlider').value,
            dehaloVal: document.getElementById('dehaloSlider').value,
            fps120: document.getElementById('fps120Toggle').checked,
            removeDeadFrames: document.getElementById('removeDeadToggle').checked,
            deadThreshold: document.getElementById('deadThresholdSlider').value,
            speedFactor: document.getElementById('speedFactorSlider').value,
            reverseRemap: document.getElementById('reverseRemapToggle').checked,
            rsmbVal: document.getElementById('rsmbSlider').value,
            shutterMode: document.getElementById('shutterDropdown').value,
            saveToDrive: document.getElementById('driveToggle').checked,
            autoDownload: document.getElementById('downloadToggle').checked,
            keepAudio: document.getElementById('audioToggle').checked,
            fixIphoneTag: document.getElementById('iphoneFixToggle').checked,
            command: "START"
        };
        google.colab.kernel.invokeFunction('notebook.command', [JSON.stringify(payload)], {});
        startPolling();
    }

    initMobileAnimationListeners();
    const observer = new MutationObserver(initMobileAnimationListeners);
    observer.observe(document.body, { childList: true, subtree: true });

    (function() {
        const dropZone = document.getElementById('dropZone');
        if (!dropZone) return;

        dropZone.addEventListener('dragenter', function(e) {
            e.preventDefault();
            dropZone.classList.add('drag-over');
        });

        dropZone.addEventListener('dragleave', function(e) {
            e.preventDefault();
            if (!dropZone.contains(e.relatedTarget)) {
                dropZone.classList.remove('drag-over');
            }
        });

        dropZone.addEventListener('dragover', function(e) {
            e.preventDefault();
        });

        dropZone.addEventListener('drop', function(e) {
            e.preventDefault();
            dropZone.classList.remove('drag-over');

            const dt = e.dataTransfer;
            const files = dt.files;
            if (files.length === 0) return;

            const input = document.createElement('input');
            input.type = 'file';
            input.multiple = true;

            const dataTransfer = new DataTransfer();
            for (let i = 0; i < files.length; i++) {
                dataTransfer.items.add(files[i]);
            }
            input.files = dataTransfer.files;

            handleMultipleFiles(input);
        });
    })();
</script>
</body>
</html>
"""

display(IPython.display.HTML(html_ui))
