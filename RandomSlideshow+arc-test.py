import os
import sys
import time
import random
import threading
import subprocess
import argparse
import tkinter as tk
import zipfile
import io
import concurrent.futures
from tkinter import ttk, messagebox, Menu
from collections import deque, OrderedDict
from pathlib import Path

# --- КОНФИГУРАЦИЯ ---
CFG_SLIDE_DURATION = 4.0
CFG_EXTENSIONS = {'.bmp', '.gif', '.jpg', '.jpeg', '.jfif', '.png', '.webp', '.ico', '.tiff'}
CFG_BG_COLOR = "#000000"
CFG_TEXT_COLOR = "#FFFFFF"
CFG_FONT = ("Segoe UI", 10)
CFG_TOOLBAR_TRIGGER_ZONE = 100
CFG_TOOLBAR_HEIGHT = 40

CFG_ARCHIVES_ENABLED = True 

# --- ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ ---

class Utils:
    @staticmethod
    def format_size(size_bytes):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0: return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.2f} TB"

class VFS:
    SEPARATOR = "::"
    PREFIX = "zip:"

    @staticmethod
    def is_virtual(path): return path.startswith(VFS.PREFIX)

    @staticmethod
    def split_zip_path(path):
        if not VFS.is_virtual(path): return None, None
        content = path[len(VFS.PREFIX):]
        if VFS.SEPARATOR in content:
            archive_path, internal_path = content.split(VFS.SEPARATOR, 1)
            return archive_path, internal_path
        return None, None

    @staticmethod
    def get_parent(path):
        if VFS.is_virtual(path):
            archive, internal = VFS.split_zip_path(path)
            internal = internal.replace('\\', '/')
            if '/' not in internal: return archive 
            parent_internal = os.path.dirname(internal)
            return f"{VFS.PREFIX}{archive}{VFS.SEPARATOR}{parent_internal}"
        else: return os.path.dirname(path)

    @staticmethod
    def get_name(path):
        if VFS.is_virtual(path): return os.path.basename(VFS.split_zip_path(path)[1])
        return os.path.basename(path)

    @staticmethod
    def get_size(path):
        try:
            if VFS.is_virtual(path):
                archive, internal = VFS.split_zip_path(path)
                with zipfile.ZipFile(archive, 'r') as zf: return zf.getinfo(internal).file_size
            else: return os.stat(path).st_size
        except: return 0

    @staticmethod
    def read_bytes(path):
        if VFS.is_virtual(path):
            archive, internal = VFS.split_zip_path(path)
            with zipfile.ZipFile(archive, 'r') as zf: return zf.read(internal)
        else:
            safe_path = str(Path(path).resolve())
            if os.name == 'nt' and not safe_path.startswith('\\\\?\\'): safe_path = '\\\\?\\' + safe_path
            with open(safe_path, 'rb') as f: return f.read()

    @staticmethod
    def list_siblings(path, extensions):
        if VFS.is_virtual(path):
            archive, internal = VFS.split_zip_path(path)
            parent_internal = os.path.dirname(internal.replace('\\', '/'))
            siblings = []
            try:
                with zipfile.ZipFile(archive, 'r') as zf:
                    for name in zf.namelist():
                        name_norm = name.replace('\\', '/')
                        if os.path.dirname(name_norm) == parent_internal:
                             if os.path.splitext(name)[1].lower() in extensions:
                                siblings.append(f"{VFS.PREFIX}{archive}{VFS.SEPARATOR}{name}")
            except: pass
            return sorted(siblings)
        else:
            parent = os.path.dirname(path)
            try:
                files = sorted([os.path.join(parent, f) for f in os.listdir(parent)
                                if os.path.splitext(f)[1].lower() in extensions])
                return files
            except OSError: return []

class ToolTip:
    def __init__(self, widget, text, delay_ms=500):
        self.widget = widget; self.text = text; self.delay_ms = delay_ms
        self._after_id = None; self._tip = None
        widget.bind("<Enter>", self._schedule, add=True)
        widget.bind("<Leave>", self._hide, add=True)
        widget.bind("<ButtonPress>", self._hide, add=True)
    def _schedule(self, _=None): self._after_id = self.widget.after(self.delay_ms, self._show)
    def _show(self):
        if self._tip or not self.text: return
        x, y = self.widget.winfo_rootx() + 10, self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget); self._tip.wm_overrideredirect(True); self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self._tip, text=self.text, bg="#111111", fg="#eeeeee", relief="solid", borderwidth=1, font=("Segoe UI", 8)).pack(ipadx=6, ipady=3)
    def _hide(self, _=None):
        if self._after_id: self.widget.after_cancel(self._after_id); self._after_id = None
        if self._tip: self._tip.destroy(); self._tip = None

try: from PIL import Image, ImageTk, ImageOps
except ImportError: messagebox.showerror("Error", "Pillow not found."); sys.exit(1)

class ImageCache:
    def __init__(self, capacity=5):
        self.capacity = capacity; self.cache = OrderedDict(); self.lock = threading.Lock()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    def get(self, key):
        with self.lock:
            if key in self.cache: self.cache.move_to_end(key); return self.cache[key]
        return None
    def put(self, key, image):
        with self.lock:
            self.cache[key] = image; self.cache.move_to_end(key)
            if len(self.cache) > self.capacity: self.cache.popitem(last=False)
    def prefetch(self, path, mode, rotation, screen_size):
        key = (path, mode, rotation, screen_size)
        if self.get(key) is None: self.executor.submit(self._decode_task, path, mode, rotation, screen_size)
    def _decode_task(self, path, mode, rotation, screen_size):
        try:
            data = VFS.read_bytes(path)
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img)
            if rotation != 0: img = img.rotate(rotation, expand=True)
            sw, sh = screen_size; iw, ih = img.size
            if mode == 0: ratio = min(sw/iw, sh/ih); tw, th = int(iw*ratio), int(ih*ratio)
            elif mode == 1: tw, th = iw, ih
            elif mode == 2: ratio = max(sw/iw, sh/ih); tw, th = int(iw*ratio), int(ih*ratio)
            elif mode == 3: ratio = min(sw/iw, sh/ih); tw, th = int(iw*ratio*4), int(ih*ratio*4)
            if tw < 1: tw = 1
            if th < 1: th = 1
            if (tw, th) != (iw, ih): img = img.resize((tw, th), Image.Resampling.LANCZOS)
            
            # [FIX] Save original dimensions to metadata so we can show them later
            img.info['original_size'] = (iw, ih)
            
            self.put((path, mode, rotation, screen_size), img)
        except: pass

class ImageLoader:
    def __init__(self): self.cache = ImageCache(capacity=6); self.current_screen_size = (1920, 1080)
    def update_screen_size(self, width, height): self.current_screen_size = (width, height)
    def load_image_sync(self, path, fit_mode, rotation):
        key = (path, fit_mode, rotation, self.current_screen_size)
        pil_img = self.cache.get(key)
        if not pil_img:
            self.cache._decode_task(path, fit_mode, rotation, self.current_screen_size)
            pil_img = self.cache.get(key)
        if pil_img: return pil_img, ImageTk.PhotoImage(pil_img)
        return None, None
    def trigger_prefetch(self, paths, fit_mode, rotation):
        for p in paths:
            if p: self.cache.prefetch(p, fit_mode, rotation, self.current_screen_size)

class SlideShowApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.parse_cli_args()
        self.title("Fast Random PySlideshow v3")
        self.geometry("1024x768")
        self.configure(bg=CFG_BG_COLOR)
        
        self.all_files = []; self.folder_set = set(); self.unviewed_indices = []
        self.viewed_paths = set(); self.history = deque(maxlen=500); self.history_pointer = -1
        self.current_path = None; self.next_random_path = None; self.current_file_index = -1
        self.is_paused = False; self.slide_timer = None; self.is_scanning_active = True
        
        self.zoom_mode = 0; self.temp_zoom = False; self.rotation = 0
        self.show_path = tk.BooleanVar(value=True); self.show_name = tk.BooleanVar(value=True)
        self.show_details = tk.BooleanVar(value=True); self.show_stats = tk.BooleanVar(value=True)
        self.toolbar_locked = True; self.fullscreen = False
        self.was_locked_before_fs = True; self.image_shown_flag = False
        
        # [FIX] State to remember resolution even if image reload fails
        self.last_valid_meta = None
        
        self.loader = ImageLoader()
        self.setup_ui()
        self.bind_events()
        self.start_initial_search()
        if self.cli_args.fullscreen: self.toggle_fullscreen()

    def parse_cli_args(self):
        global CFG_ARCHIVES_ENABLED
        parser = argparse.ArgumentParser()
        parser.add_argument("path", nargs="?", default=None)
        parser.add_argument("--cwd", action="store_true")
        parser.add_argument("--fullscreen", action="store_true")
        g = parser.add_mutually_exclusive_group()
        g.add_argument("--includeacr", action="store_true"); g.add_argument("--excludeacr", action="store_true")
        self.cli_args = parser.parse_args()
        if self.cli_args.cwd: self.root_dir = os.getcwd()
        elif self.cli_args.path: self.root_dir = os.path.abspath(self.cli_args.path)
        else: self.root_dir = str(Path(__file__).resolve().parent)
        if self.cli_args.includeacr: CFG_ARCHIVES_ENABLED = True
        elif self.cli_args.excludeacr: CFG_ARCHIVES_ENABLED = False

    def setup_ui(self):
        self.canvas = tk.Canvas(self, bg=CFG_BG_COLOR, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.toolbar = tk.Frame(self, bg="#333333", height=CFG_TOOLBAR_HEIGHT)
        self.toolbar.pack_propagate(False)
        self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)
        style = ttk.Style(); style.theme_use('clam'); style.configure("TButton", font=("Segoe UI", 9), padding=2)
        def btn(t, c, w=None, tt=""):
            b = ttk.Button(self.toolbar, text=t, command=c, width=w); b.pack(side='left', padx=2)
            if tt: ToolTip(b, tt)
            return b
        btn("?", self.show_help, 2, "Help (F1)")
        self.btn_lock = btn("FIX", self.toggle_toolbar_lock, 6)
        self.btn_full = btn("FULL", self.toggle_fullscreen, 5)
        self.btn_zoom = btn("ZOOM Fit", self.cycle_zoom, 10)
        btn("CCR", lambda: self.rotate_image(-90), 4); btn("CR", lambda: self.rotate_image(90), 4)
        btn("<--", self.prev_image, 4); btn("-->", self.next_image, 4)
        btn("<<", self.first_file_folder, 3); btn("<-", self.prev_file_alpha, 3); btn("->", self.next_file_alpha, 3)
        self.btn_play = btn("PAUSE", self.toggle_pause, 6)
        tk.Label(self.toolbar, text="Sec:", bg="#333333", fg="white").pack(side='left', padx=(5,0))
        self.speed_var = tk.StringVar(value=str(CFG_SLIDE_DURATION)); self.speed_var.trace("w", self.on_speed_change)
        tk.Entry(self.toolbar, textvariable=self.speed_var, width=4).pack(side='left', padx=2)
        btn("FOLDER", self.open_current_folder, 7)
        self.lbl_info = tk.Label(self.toolbar, text="Init...", bg="#333333", fg=CFG_TEXT_COLOR, font=CFG_FONT, anchor='e')
        self.lbl_info.pack(side='right', padx=10, fill='x', expand=True)
        self.lbl_info.bind("<Button-1>", lambda e: self.cycle_info_preset())
        self.lbl_info.bind("<Button-3>", self.show_info_menu)

    def bind_events(self):
        self.bind("<Right>", lambda e: self.next_image()); self.bind("<Left>", lambda e: self.prev_image())
        self.bind("<space>", lambda e: self.toggle_pause())
        self.bind("<Up>", lambda e: self.prev_file_alpha()); self.bind("<Down>", lambda e: self.next_file_alpha())
        self.bind("<Home>", lambda e: self.first_file_folder())
        self.bind("<Prior>", lambda e: self.nav_folder_prev()); self.bind("<Next>", lambda e: self.nav_folder_next())
        self.bind("<Return>", lambda e: self.open_current_folder())
        self.bind("<F1>", lambda e: self.show_help())
        self.bind("<Escape>", lambda e: self.toggle_fullscreen(force_exit=True))
        self.bind("<F11>", lambda e: self.toggle_fullscreen()); self.bind("<Alt-Return>", lambda e: self.toggle_fullscreen())
        self.bind("z", lambda e: self.cycle_zoom()); self.bind("Z", lambda e: self.cycle_zoom())
        self.bind("<KeyPress-Shift_L>", self.enable_temp_zoom); self.bind("<KeyRelease-Shift_L>", self.disable_temp_zoom)
        self.bind("<KeyPress-Shift_R>", self.enable_temp_zoom); self.bind("<KeyRelease-Shift_R>", self.disable_temp_zoom)
        self.bind("<Control-r>", lambda e: self.rotate_image(90)); self.bind("<Control-e>", lambda e: self.rotate_image(-90))
        self.bind("i", lambda e: self.cycle_info_preset())
        self.canvas.bind("<Motion>", self.check_toolbar_hover); self.bind("<Tab>", lambda e: self.toggle_toolbar_lock())
        self.canvas.bind("<Button-3>", self.show_context_menu)
        self.canvas.bind("<B1-Motion>", self.on_canvas_motion, add=True)
        self.canvas.bind("<Motion>", self.on_canvas_motion, add=True) 
        self.bind("<Configure>", self.on_resize)

    def start_initial_search(self): threading.Thread(target=self.find_first_image_task, daemon=True).start()
    def find_first_image_task(self):
        self.find_random_image_dynamic(initial=True); time.sleep(0.5)
        threading.Thread(target=self.scan_worker, daemon=True).start()

    def find_random_image_dynamic(self, initial=False):
        try:
            current = self.root_dir
            if initial: print(f"\n=== START INITIAL SCAN (Root: {current}) ===") 
            
            for i in range(50):
                if initial and self.image_shown_flag: 
                    if initial: print("-> [Status] Image already shown. Stopping scan.")
                    return
                
                if initial: print(f"\n[Step {i}] Current Location: {current}")
                if initial and i % 5 == 0: self.lbl_info.config(text=f"Scanning: {os.path.basename(current)}...")
                
                try: 
                    entries = list(os.scandir(current))
                except Exception as e:
                    if initial: print(f"-> [Error] Failed to scan directory: {e}") 
                    # CRITICAL FIX: If scanning fails (bad zip, permission error), reset to root and retry
                    # instead of breaking the loop completely.
                    if initial and not self.image_shown_flag:
                        print("-> [Recovery] Resetting search to root directory...")
                        current = self.root_dir
                        continue
                    break
                
                dirs = []; files = []
                for e in entries:
                    if e.is_dir(): dirs.append(e.path)
                    elif e.is_file():
                        ext = os.path.splitext(e.name)[1].lower()
                        if ext in CFG_EXTENSIONS: files.append(e.path)
                        elif CFG_ARCHIVES_ENABLED and ext == '.zip': dirs.append(e.path)
                
                unseen = [f for f in files if f not in self.viewed_paths]
                if initial: print(f"-> [Found] Directories: {len(dirs)} | Files: {len(files)} | Unseen Candidates: {len(unseen)}")
                
                pick_here = False
                if unseen:
                    rnd = random.random()
                    threshold = 0.25
                    is_forced = (not dirs)
                    
                    if initial:
                        if is_forced: print(f"-> [Decision] No subdirectories. FORCE PICK.")
                        elif rnd < threshold: print(f"-> [Decision] Random {rnd:.3f} < {threshold}. PICK HERE.")
                        else: print(f"-> [Decision] Random {rnd:.3f} >= {threshold}. DIVE DEEPER.")

                    if is_forced or rnd < threshold: pick_here = True
                
                if pick_here and unseen:
                    t = random.choice(unseen)
                    if initial: print(f"-> [Action] Selected file: {t}")
                    self.after(0, lambda p=t: self.load_dynamic_result(p, initial))
                    return
                
                if dirs:
                    ch = random.choice(dirs)
                    if initial: print(f"-> [Action] Jumping to folder: {ch}")
                    
                    if CFG_ARCHIVES_ENABLED and ch.lower().endswith('.zip'):
                        if initial: print(f"-> [Archive] Attempting to pick from ZIP...")
                        if self.try_pick_from_zip(ch, initial): 
                            if initial: print(f"-> [Archive] Success! Image picked from ZIP.")
                            return
                        
                        if initial: print(f"-> [Archive] Failed to pick from ZIP. Setting ZIP as current path.")
                        current = ch 
                    else: 
                        current = ch
                else: 
                    if initial: print("-> [Stop] No subdirectories to dive into and no files chosen. Scan ends.")
                    break
        except Exception as e: 
            if initial: print(f"-> [Exception] Critical error in scanner: {e}")
            pass

    def try_pick_from_zip(self, zp, initial):
        try:
            with zipfile.ZipFile(zp, 'r') as zf:
                names = [n for n in zf.namelist() if os.path.splitext(n)[1].lower() in CFG_EXTENSIONS]
                if names:
                    p = f"{VFS.PREFIX}{zp}{VFS.SEPARATOR}{random.choice(names)}"
                    if p not in self.viewed_paths:
                        self.after(0, lambda: self.load_dynamic_result(p, initial)); return True
        except: pass
        return False

    def load_dynamic_result(self, p, initial):
        if initial and self.image_shown_flag: return
        if p in self.viewed_paths: return
        self.load_by_path(p)
        if not self.history: self.history.append(p); self.history_pointer = 0
        if not self.is_paused: self.schedule_next_slide()

    def scan_worker(self):
        temp = []; last = time.time()
        def flush():
            nonlocal temp, last
            if temp: self.after(0, lambda b=list(temp): self.add_batch(b)); temp = []; last = time.time()
        for root, dirs, files in os.walk(self.root_dir):
            random.shuffle(dirs)
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                fp = os.path.join(root, f)
                if ext in CFG_EXTENSIONS: temp.append(fp)
                elif CFG_ARCHIVES_ENABLED and ext == '.zip':
                    try:
                        with zipfile.ZipFile(fp, 'r') as zf:
                            for n in zf.namelist():
                                if os.path.splitext(n)[1].lower() in CFG_EXTENSIONS:
                                    temp.append(f"{VFS.PREFIX}{fp}{VFS.SEPARATOR}{n}")
                    except: pass
            if len(temp) > 1000 or (time.time() - last > 0.5 and temp): flush()
        flush(); self.is_scanning_active = False

    def add_batch(self, b):
        s = len(self.all_files); self.all_files.extend(b)
        self.unviewed_indices.extend(range(s, s + len(b)))
        for p in b: self.folder_set.add(VFS.get_parent(p))
        if self.image_shown_flag and self.show_stats.get(): self.update_info_label(None)

    def prepare_next_random(self):
        idx = self.get_random_index()
        self.next_random_path = self.all_files[idx] if idx != -1 else None
        return self.next_random_path

    def get_random_index(self):
        if not self.all_files: return -1
        if not self.unviewed_indices: self.unviewed_indices = list(range(len(self.all_files)))
        for _ in range(10):
            if not self.unviewed_indices: return -1
            ri = random.randrange(len(self.unviewed_indices))
            val = self.unviewed_indices[ri]; self.unviewed_indices[ri] = self.unviewed_indices[-1]; self.unviewed_indices.pop()
            if self.all_files[val] not in self.viewed_paths: return val
        return -1

    def next_image(self):
        if self.history_pointer < len(self.history) - 1:
            self.history_pointer += 1; self.load_by_path(self.history[self.history_pointer]); return
        if self.next_random_path:
            p = self.next_random_path; self.next_random_path = None
            self.load_by_path(p); self.history.append(p); self.history_pointer = len(self.history) - 1; return
        if self.is_scanning_active and len(self.all_files) < 2000:
            threading.Thread(target=self.find_random_image_dynamic, args=(False,), daemon=True).start()
        else:
            idx = self.get_random_index()
            if idx != -1:
                p = self.all_files[idx]; self.load_by_path(p); self.history.append(p); self.history_pointer = len(self.history) - 1
            else: threading.Thread(target=self.find_random_image_dynamic, args=(False,), daemon=True).start()

    def prev_image(self):
        if self.history_pointer > 0: self.history_pointer -= 1; self.load_by_path(self.history[self.history_pointer])

    def load_by_path(self, path):
        self.current_path = path; self.viewed_paths.add(path); self.rotation = 0
        # [FIX] Reset cached metadata for new file
        self.last_valid_meta = None 
        self.display_current_image(); self.reset_timer()
        threading.Thread(target=self.schedule_prefetches, args=(path,), daemon=True).start()

    def schedule_prefetches(self, path):
        nxt = self.prepare_next_random(); sibs = VFS.list_siblings(path, CFG_EXTENSIONS)
        pa = None; na = None
        if sibs:
            try:
                i = sibs.index(path)
                pa = sibs[(i-1)%len(sibs)]; na = sibs[(i+1)%len(sibs)]
            except: pass
        self.loader.trigger_prefetch([nxt, pa, na], self.zoom_mode, 0)

    def nav_sibling(self, off):
        if not self.current_path: return
        sibs = VFS.list_siblings(self.current_path, CFG_EXTENSIONS)
        if not sibs: return
        try:
            i = sibs.index(self.current_path); p = sibs[(i+off)%len(sibs)]
            self.load_by_path(p)
            if not self.history or self.history[-1] != p: self.history.append(p); self.history_pointer = len(self.history)-1
        except:
            if sibs: self.load_by_path(sibs[0])
    def next_file_alpha(self): self.nav_sibling(1)
    def prev_file_alpha(self): self.nav_sibling(-1)
    def first_file_folder(self):
        if self.current_path:
            s = VFS.list_siblings(self.current_path, CFG_EXTENSIONS)
            if s:
                self.load_by_path(s[0])
                if not self.history or self.history[-1] != s[0]: self.history.append(s[0]); self.history_pointer=len(self.history)-1

    def _folder_key(self, p):
        if p.startswith(VFS.PREFIX):
            a, i = VFS.split_zip_path(p)
            return (os.path.normpath(a).lower(), i.replace('\\', '/').lower())
        return (os.path.normpath(p).lower(), "")

    def nav_folder_step(self, off):
        if not self.current_path: return
        cur = VFS.get_parent(self.current_path); fs = list(self.folder_set)
        if not fs: return
        fs.sort(key=self._folder_key)
        try:
            i = fs.index(cur); target = fs[(i+off)%len(fs)]
            self._load_rnd_in(target)
        except: self._load_rnd_in(fs[0])
    def nav_folder_next(self): self.nav_folder_step(1)
    def nav_folder_prev(self): self.nav_folder_step(-1)

    def _load_rnd_in(self, fld):
        files = []
        if VFS.is_virtual(fld):
            a, i = VFS.split_zip_path(fld + VFS.SEPARATOR + "x"); i = i.replace('\\', '/')
            try:
                with zipfile.ZipFile(a, 'r') as zf:
                    for n in zf.namelist():
                        if os.path.dirname(n.replace('\\','/'))==i and os.path.splitext(n)[1].lower() in CFG_EXTENSIONS:
                            files.append(f"{VFS.PREFIX}{a}{VFS.SEPARATOR}{n}")
            except: pass
        else:
            if CFG_ARCHIVES_ENABLED and os.path.isfile(fld) and fld.lower().endswith('.zip'):
                try:
                    with zipfile.ZipFile(fld, 'r') as zf:
                        for n in zf.namelist():
                            if '/' not in n and '\\' not in n and os.path.splitext(n)[1].lower() in CFG_EXTENSIONS:
                                files.append(f"{VFS.PREFIX}{fld}{VFS.SEPARATOR}{n}")
                except: pass
            elif os.path.isdir(fld):
                try: files = [os.path.join(fld,x) for x in os.listdir(fld) if os.path.splitext(x)[1].lower() in CFG_EXTENSIONS]
                except: pass
        if files:
            t = random.choice(files); self.load_by_path(t)
            if not self.history or self.history[-1]!=t: self.history.append(t); self.history_pointer=len(self.history)-1

    def display_current_image(self):
        if not self.current_path: return
        self.image_shown_flag = True
        mode = 3 if self.temp_zoom else self.zoom_mode
        self.loader.update_screen_size(self.winfo_width(), self.winfo_height())
        pil, tk_img = self.loader.load_image_sync(self.current_path, mode, self.rotation)
        self.canvas.delete("all")
        
        # [FIX] Persist metadata if load successful
        if pil:
            self.last_valid_meta = pil.info.get('original_size', (pil.width, pil.height))

        if not pil:
            self.canvas.create_text(self.winfo_width()//2, self.winfo_height()//2, text="Error/Loading...", fill="white")
            self.update_info_label(None); return
        self.current_tk_image = tk_img
        self.canvas.create_image(self.winfo_width()//2, self.winfo_height()//2, image=tk_img, anchor='center', tags='img')
        
        if pil.width > self.winfo_width() or pil.height > self.winfo_height():
            self.update_zoom_pan()
            
        self.update_info_label(pil)

    def update_info_label(self, img):
        if not self.current_path: self.lbl_info.config(text=""); return
        p = [VFS.get_parent(self.current_path) + " \\"]
        if self.show_name.get(): p.append(VFS.get_name(self.current_path))
        if self.show_details.get():
            sz = Utils.format_size(VFS.get_size(self.current_path))
            
            # [FIX] Use stored original resolution if available, instead of screen-resized resolution
            w, h = None, None
            if img:
                w, h = img.info.get('original_size', (img.width, img.height))
            elif self.last_valid_meta:
                w, h = self.last_valid_meta
            
            res_str = f"[{w}x{h}]" if (w and h) else "[???]"
            p.append(f"{res_str} [{sz}]")
            
        if self.show_stats.get():
            v = len(self.viewed_paths); t = max(v, len(self.all_files))
            p.append(f"({v} of {t} in {len(self.folder_set)})")
        self.lbl_info.config(text=" ".join(p))

    def on_resize(self, e):
        if hasattr(self, '_rj'): self.after_cancel(self._rj)
        self._rj = self.after(100, self.display_current_image)

    def cycle_zoom(self):
        m = ["ZOOM Fit", "ZOOM Orig", "ZOOM Fill"]
        self.zoom_mode = (self.zoom_mode + 1) % 3
        self.btn_zoom.config(text=m[self.zoom_mode])
        self.display_current_image(); self.reset_timer()

    def enable_temp_zoom(self, e):
        if not self.temp_zoom: self.temp_zoom = True; self.display_current_image(); self.reset_timer()
    def disable_temp_zoom(self, e):
        if self.temp_zoom: self.temp_zoom = False; self.display_current_image(); self.reset_timer()

    def on_canvas_motion(self, event):
        if hasattr(self, 'current_tk_image') and self.current_tk_image:
            w, h = self.winfo_width(), self.winfo_height()
            iw, ih = self.current_tk_image.width(), self.current_tk_image.height()
            
            if iw <= w and ih <= h:
                self.canvas.coords('img', w/2, h/2)
                return

            cx, cy = w/2, h/2
            if iw > w:
                ratio_x = event.x / w
                cx = -(iw - w) * ratio_x + iw/2
            if ih > h:
                ratio_y = event.y / h
                cy = -(ih - h) * ratio_y + ih/2
            
            self.canvas.coords('img', cx, cy)

    def update_zoom_pan(self):
        x = self.winfo_pointerx() - self.winfo_rootx()
        y = self.winfo_pointery() - self.winfo_rooty()
        class E: pass
        e = E(); e.x, e.y = x, y
        self.on_canvas_motion(e)

    def rotate_image(self, d): self.rotation = (self.rotation - d) % 360; self.display_current_image(); self.reset_timer()
    
    def show_help(self): messagebox.showinfo("Help", "Arrows, Space, Z, Shift, PgUp/PgDn, F11")
    def show_context_menu(self, e):
        m = Menu(self, tearoff=0); m.add_command(label="Next", command=self.next_image)
        m.add_command(label="Pause", command=self.toggle_pause); m.add_separator()
        m.add_command(label="Folder", command=self.open_current_folder); m.tk_popup(e.x_root, e.y_root)
    def show_info_menu(self, e):
        m = Menu(self, tearoff=0)
        for l, v in [("Name",self.show_name), ("Path",self.show_path), ("Det",self.show_details), ("Stat",self.show_stats)]:
            m.add_checkbutton(label=l, variable=v, command=self.display_current_image)
        m.tk_popup(e.x_root, e.y_root)
    def cycle_info_preset(self):
        n, p, d = self.show_name.get(), self.show_path.get(), self.show_details.get()
        if n and p and d: self.show_path.set(0); self.show_details.set(0); self.show_stats.set(0)
        elif n and not p: self.show_path.set(1)
        elif p: self.show_name.set(0); self.show_path.set(0)
        else: self.show_name.set(1); self.show_path.set(1); self.show_details.set(1); self.show_stats.set(1)
        self.display_current_image()
    
    def check_toolbar_hover(self, e):
        if self.toolbar_locked: return
        ry = self.winfo_rooty(); py = self.winfo_pointery()
        if py < ry or py > ry + self.winfo_height(): return
        y_pos = 0 if (self.winfo_height() - (py - ry) < CFG_TOOLBAR_TRIGGER_ZONE) else 100
        self.toolbar.place(relx=0, rely=1.0, y=y_pos, anchor='sw', relwidth=1.0)
    
    def toggle_toolbar_lock(self):
        self.toolbar_locked = not self.toolbar_locked; self.btn_lock.config(text="HIDE" if self.toolbar_locked else "SHOW")
        if self.toolbar_locked: self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)
    def toggle_fullscreen(self, force_exit=False):
        if force_exit: self.fullscreen = False
        else: self.fullscreen = not self.fullscreen
        self.attributes("-fullscreen", self.fullscreen)
        if self.fullscreen: self.was_locked_before_fs = self.toolbar_locked; self.toolbar_locked = False
        else: self.overrideredirect(False); self.toolbar_locked = self.was_locked_before_fs
        self.btn_lock.config(text="HIDE" if self.toolbar_locked else "SHOW")
        if self.toolbar_locked: self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)

    def toggle_pause(self):
        self.is_paused = not self.is_paused; self.btn_play.config(text="PLAY" if self.is_paused else "PAUSE")
        if not self.is_paused: self.schedule_next_slide()
        elif self.slide_timer: self.after_cancel(self.slide_timer); self.slide_timer = None
    def on_speed_change(self, *a):
        try: 
            if float(self.speed_var.get().replace(',','.')) <= 0: raise ValueError
        except: self.is_paused = True; self.btn_play.config(text="PLAY")
    def schedule_next_slide(self):
        if self.slide_timer: self.after_cancel(self.slide_timer)
        if self.is_paused: return
        try: s = float(self.speed_var.get().replace(',', '.'))
        except: s = 4.0
        self.slide_timer = self.after(int(s*1000), self.auto_next)
    def auto_next(self):
        if not self.is_paused: self.next_image(); self.schedule_next_slide()
    def reset_timer(self): self.schedule_next_slide()
    def open_current_folder(self):
        if not self.current_path: return
        self.is_paused = True; self.btn_play.config(text="PLAY")
        p = VFS.split_zip_path(self.current_path)[0] if VFS.is_virtual(self.current_path) else self.current_path
        p = os.path.normpath(p)
        try:
            if os.name == 'nt': subprocess.run(['explorer', '/select,', p])
            else: subprocess.run(['xdg-open', os.path.dirname(p)])
        except: pass

if __name__ == "__main__":
    app = SlideShowApp()
    if os.name == 'nt':
        try: app.state('zoomed')
        except: pass
    else: app.attributes('-zoomed', True)
    app.mainloop()
