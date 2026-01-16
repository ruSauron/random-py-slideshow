import os
import sys
import time
import random
import threading
import subprocess
import argparse
import tkinter as tk
from tkinter import ttk, messagebox, Menu
from collections import deque
from pathlib import Path

# --- Configuration (Edit these) ---
CFG_SLIDE_DURATION = 4.0      # Default seconds per slide
CFG_EXTENSIONS = {'.bmp', '.gif', '.jpg', '.jpeg', '.jfif', '.png', '.webp', '.ico', '.tiff'}
CFG_BG_COLOR = "#000000"
CFG_TEXT_COLOR = "#FFFFFF"
CFG_FONT = ("Segoe UI", 10)
CFG_TOOLBAR_TRIGGER_ZONE = 100 # Pixels from bottom to show toolbar
CFG_TOOLBAR_HEIGHT = 40
CFG_CACHE_SIZE = 5            # Images to keep in memory

# --- Utils ---
class Utils:
    @staticmethod
    def format_size(size_bytes):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.2f} TB"

class ToolTip:
    """Simple ToolTip for Tkinter widgets."""
    def __init__(self, widget, text, delay_ms=500):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add=True)
        widget.bind("<Leave>", self._hide, add=True)
        widget.bind("<ButtonPress>", self._hide, add=True)

    def _schedule(self, _=None):
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _show(self):
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(self._tip, text=self.text, bg="#111111", fg="#eeeeee",
                       relief="solid", borderwidth=1, font=("Segoe UI", 8))
        lbl.pack(ipadx=6, ipady=3)

    def _hide(self, _=None):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip:
            self._tip.destroy()
            self._tip = None

# --- Image Loader (Background Processing) ---
try:
    from PIL import Image, ImageTk, ImageOps
except ImportError:
    messagebox.showerror("Error", "Pillow library not found.\nPlease install it: pip install Pillow")
    sys.exit(1)

class ImageLoader:
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()
        self.current_screen_size = (1920, 1080)

    def update_screen_size(self, width, height):
        self.current_screen_size = (width, height)

    def load_image(self, path, fit_mode, rotation, force_reload=False):
        """
        Loads, rotates, and resizes image.
        fit_mode: 0=Fit (contain), 1=Original, 2=Fill (cover), 3=4x Zoom
        """
        # Unique key for caching logic
        cache_key = (path, fit_mode, rotation, self.current_screen_size)
        
        with self.lock:
            if not force_reload and cache_key in self.cache:
                return self.cache[cache_key]

        try:
            safe_path = str(Path(path).resolve())
            # Windows long path fix
            if os.name == 'nt' and not safe_path.startswith('\\\\?\\'):
                safe_path = '\\\\?\\' + safe_path

            img = Image.open(safe_path)
            img = ImageOps.exif_transpose(img) # Fix orientation from EXIF

            if rotation != 0:
                img = img.rotate(rotation, expand=True)

            sw, sh = self.current_screen_size
            iw, ih = img.size
            target_w, target_h = iw, ih

            # Calc target dimensions
            if fit_mode == 0: # Fit
                ratio = min(sw/iw, sh/ih)
                target_w, target_h = int(iw * ratio), int(ih * ratio)
            elif fit_mode == 1: # Original
                pass 
            elif fit_mode == 2: # Fill
                ratio = max(sw/iw, sh/ih)
                target_w, target_h = int(iw * ratio), int(ih * ratio)
            elif fit_mode == 3: # 4x Zoom
                target_w, target_h = iw * 4, ih * 4

            # Safety check
            if target_w < 1: target_w = 1
            if target_h < 1: target_h = 1

            # Resize (High quality)
            # Optimization: Don't resize if size is same
            if (target_w, target_h) != (iw, ih):
                 img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
            
            tk_img = ImageTk.PhotoImage(img)

            with self.lock:
                # LRU Cache cleanup
                if len(self.cache) >= CFG_CACHE_SIZE:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[cache_key] = (img, tk_img)
            
            return img, tk_img

        except Exception as e:
            print(f"Error loading {path}: {e}")
            return None, None

# --- Main Application ---
class SlideShowApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.parse_cli_args()

        self.title("Fast Random PySlideshow")
        self.geometry("1024x768")
        self.configure(bg=CFG_BG_COLOR)
        
        # State
        self.all_files = []           # List of all found file paths
        self.unviewed_indices = []    # Indices in all_files not yet viewed
        self.history = deque(maxlen=500) # History of indices
        self.history_pointer = -1     # Current position in history
        self.current_path = None
        self.current_file_index = -1
        
        self.is_paused = False
        self.slide_timer = None
        self.is_scanning_active = True # Flag to track scan status
        
        # View settings
        self.zoom_mode = 0  # 0=Fit, 1=Orig, 2=Fill, 3=4x
        self.temp_zoom = False
        self.rotation = 0
        self.info_mode = 0 # 0=All, 1=Name, 2=Full+Path, 3=Hidden
        
        # UI State
        self.toolbar_locked = True
        self.fullscreen = False
        self.w_state_before_full = 'zoomed'
        self.was_locked_before_fs = True
        self.image_shown_flag = False # True if at least one image is shown

        self.loader = ImageLoader()
        
        self.setup_ui()
        self.bind_events()
        
        # Start workers
        self.start_threads()
        
        # Apply CLI flags
        if self.cli_args.fullscreen:
            self.toggle_fullscreen()
        
    def parse_cli_args(self):
        parser = argparse.ArgumentParser(description="Fast Random Image Slideshow")
        parser.add_argument("path", nargs="?", default=None, help="Root folder to scan")
        parser.add_argument("--cwd", action="store_true", help="Use current working directory")
        parser.add_argument("--fullscreen", action="store_true", help="Start in fullscreen mode")
        parser.add_argument("--windowed", action="store_true", help="Start in windowed mode")
        self.cli_args = parser.parse_args()

        if self.cli_args.cwd:
            self.root_dir = os.getcwd()
        elif self.cli_args.path:
            self.root_dir = os.path.abspath(self.cli_args.path)
        else:
            # Default: Script folder
            self.root_dir = str(Path(__file__).resolve().parent)

    def setup_ui(self):
        # Canvas
        self.canvas = tk.Canvas(self, bg=CFG_BG_COLOR, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        
        # Toolbar
        self.toolbar = tk.Frame(self, bg="#333333", height=CFG_TOOLBAR_HEIGHT)
        self.toolbar.pack_propagate(False)
        self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)

        # Styles
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TButton", font=("Segoe UI", 9), padding=2)

        # Buttons
        def btn(text, cmd, width=None, tooltip=""):
            b = ttk.Button(self.toolbar, text=text, command=cmd, width=width)
            b.pack(side='left', padx=2)
            if tooltip: ToolTip(b, tooltip)
            return b

        btn("?", self.show_help, 2, "Help (F1)")
        self.btn_lock = btn("FIX", self.toggle_toolbar_lock, 6, "Lock/Unlock Toolbar")
        self.btn_full = btn("FULL", self.toggle_fullscreen, 5, "Fullscreen (F11 / Alt+Enter)")
        self.btn_zoom = btn("ZOOM Fit", self.cycle_zoom, 10, "Zoom Mode (Z)\nFit -> Orig -> Fill -> 4x")
        btn("CCR", lambda: self.rotate_image(-90), 4, "Rotate Left (Ctrl+E)")
        btn("CR", lambda: self.rotate_image(90), 4, "Rotate Right (Ctrl+R)")

        # Right side info
        self.lbl_info = tk.Label(self.toolbar, text="", bg="#333333", fg=CFG_TEXT_COLOR, 
                                 font=CFG_FONT, anchor='e')
        self.lbl_info.pack(side='right', padx=10, fill='x', expand=True)
        self.lbl_info.bind("<Button-1>", lambda e: self.cycle_info_mode())
        ToolTip(self.lbl_info, "File Info (Click to cycle modes, 'I')")

        # Right side controls (packed right to left)
        def btn_r(text, cmd, width=None, tooltip=""):
            b = ttk.Button(self.toolbar, text=text, command=cmd, width=width)
            b.pack(side='right', padx=2)
            if tooltip: ToolTip(b, tooltip)
            return b

        btn_r("FOLDER", self.open_current_folder, 7, "Open File Location (Enter)")
        
        # Speed Control
        self.btn_play = btn_r("PAUSE", self.toggle_pause, 8, "Play/Pause (Space)")
        tk.Label(self.toolbar, text="sec", bg="#333333", fg="white").pack(side='right')
        
        self.speed_var = tk.StringVar(value=str(CFG_SLIDE_DURATION))
        self.speed_var.trace_add('write', self.on_speed_change)
        self.ent_speed = ttk.Entry(self.toolbar, textvariable=self.speed_var, width=4, justify='center')
        self.ent_speed.pack(side='right', padx=2)
        ToolTip(self.ent_speed, "Slideshow delay in seconds")

        # Navigation
        btn_r("-->", self.next_image, 4, "Next Random Image (Right Arrow)")
        btn_r("<--", self.prev_image, 4, "Previous History Image (Left Arrow)")
        
        btn_r(">>", self.next_file_alpha, 3, "Next File in Folder (PgDn)")
        btn_r("<<", self.prev_file_alpha, 3, "Prev File in Folder (PgUp)")
        btn_r("|<", self.first_file_folder, 3, "First File in Folder (Home)")

    def bind_events(self):
        self.bind("<Motion>", self.check_toolbar_hover)
        self.canvas.bind("<Button-3>", self.show_context_menu)
        
        self.bind("<Escape>", lambda e: self.toggle_fullscreen(force_exit=True))
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.bind("<Alt-Return>", lambda e: self.toggle_fullscreen())
        self.bind("<F1>", lambda e: self.show_help())
        
        self.bind("z", lambda e: self.cycle_zoom())
        self.bind("Z", lambda e: self.cycle_zoom())
        self.bind("<Shift_L>", self.enable_temp_zoom)
        self.bind("<KeyRelease-Shift_L>", self.disable_temp_zoom)
        
        self.bind("<Control-e>", lambda e: self.rotate_image(-90))
        self.bind("<Control-r>", lambda e: self.rotate_image(90))
        
        self.bind("i", lambda e: self.cycle_info_mode())
        
        self.bind("<Left>", lambda e: self.prev_image())
        self.bind("<Right>", lambda e: self.next_image())
        
        self.bind("<Home>", lambda e: self.first_file_folder())
        self.bind("<Prior>", lambda e: self.prev_file_alpha()) # PgUp
        self.bind("<Next>", lambda e: self.next_file_alpha())  # PgDn
        
        self.bind("<space>", lambda e: self.toggle_pause())
        self.bind("<Return>", lambda e: self.open_current_folder())
        
        self.canvas.bind("<Motion>", self.on_canvas_motion)
        self.bind("<Configure>", self.on_resize)

    def start_threads(self):
        # Full scanner
        t_scan = threading.Thread(target=self.scan_worker, daemon=True)
        t_scan.start()
        
        # Initial quick find
        threading.Thread(target=self.find_random_image_dynamic, args=(True,), daemon=True).start()

    # --- Scanning & Search Logic ---
    
    def find_random_image_dynamic(self, initial=False):
        """
        Performs a 'random walk' through the file system to find ONE random image.
        This is used when the full file tree is not yet built to ensure randomness.
        """
        try:
            current = self.root_dir
            # Safety limiter
            for _ in range(50):
                # If we have shown something and it's initial call, stop
                if initial and self.image_shown_flag: return 
                
                try:
                    entries = list(os.scandir(current))
                except (OSError, PermissionError):
                    break # Cannot read, stop walk
                
                # Separate dirs and files
                dirs = [e.path for e in entries if e.is_dir()]
                files = [e.path for e in entries if e.is_file() 
                         and os.path.splitext(e.name)[1].lower() in CFG_EXTENSIONS]
                
                # Logic:
                # 1. If we found files, small chance to pick one immediately.
                # 2. If no files, must go deeper.
                # 3. If no dirs, must pick file here or fail.
                
                pick_here = False
                if files:
                    if not dirs: pick_here = True
                    elif random.random() < 0.25: pick_here = True # 25% chance to stop at this folder
                
                if pick_here and files:
                    pick = random.choice(files)
                    # Schedule UI update
                    self.after(0, lambda p=pick: self.load_dynamic_result(p, initial))
                    return
                
                if dirs:
                    current = random.choice(dirs)
                else:
                    break # Dead end
        except Exception as e:
            print(f"Dynamic walker error: {e}")

    def load_dynamic_result(self, path, initial):
        # Called from main thread via after()
        if initial and self.image_shown_flag: return
        
        # If we are already displaying this exact image, ignore (unlikely with random)
        if self.current_path == path: return

        self.load_by_path(path)
        
        # Ensure timer is running if auto-play is on
        if not self.is_paused:
            self.schedule_next_slide()

    def scan_worker(self):
        """Background full tree scan."""
        temp_batch = []
        last_update = time.time()
        
        for root, dirs, files in os.walk(self.root_dir):
            random.shuffle(dirs) # Randomize traversal order
            
            for f in files:
                if os.path.splitext(f)[1].lower() in CFG_EXTENSIONS:
                    full_path = os.path.join(root, f)
                    temp_batch.append(full_path)
            
            if len(temp_batch) > 1000 or (time.time() - last_update > 0.5 and temp_batch):
                self.after(0, lambda b=list(temp_batch): self.add_files_batch(b))
                temp_batch = []
                last_update = time.time()
        
        if temp_batch:
            self.after(0, lambda b=list(temp_batch): self.add_files_batch(b))
            
        self.is_scanning_active = False # Done

    def add_files_batch(self, batch):
        start_idx = len(self.all_files)
        self.all_files.extend(batch)
        new_indices = list(range(start_idx, start_idx + len(batch)))
        self.unviewed_indices.extend(new_indices)

    # --- Navigation Logic ---

    def get_random_index(self):
        if not self.all_files: return -1
        if not self.unviewed_indices:
            self.unviewed_indices = list(range(len(self.all_files)))
        
        if not self.unviewed_indices: return -1
        
        rnd_idx = random.randrange(len(self.unviewed_indices))
        val = self.unviewed_indices[rnd_idx]
        self.unviewed_indices[rnd_idx] = self.unviewed_indices[-1]
        self.unviewed_indices.pop()
        
        return val

    def goto_index(self, index, record_history=True):
        if index < 0 or index >= len(self.all_files): return
        
        path = self.all_files[index]
        self.current_file_index = index
        self.current_path = path
        
        if record_history:
            self.history.append(index)
            self.history_pointer = len(self.history) - 1
        
        self.rotation = 0
        self.display_current_image()
        self.reset_timer()

    def next_image(self):
        # 1. History forward
        if self.history_pointer < len(self.history) - 1:
            self.history_pointer += 1
            idx = self.history[self.history_pointer]
            self.goto_index(idx, False)
            return

        # 2. New random image
        # While scanning is active, we rely heavily on dynamic walk to visit unindexed folders
        should_dynamic_walk = False
        
        if self.is_scanning_active:
            # 80% chance to random walk if scanning, to avoid getting stuck in first folders
            if random.random() < 0.8:
                should_dynamic_walk = True
            elif not self.all_files:
                should_dynamic_walk = True
                
        if not self.all_files and not should_dynamic_walk:
            should_dynamic_walk = True

        if should_dynamic_walk:
            threading.Thread(target=self.find_random_image_dynamic, args=(False,), daemon=True).start()
        else:
            # Tree built or we hit the 20% chance to use known files
            idx = self.get_random_index()
            if idx != -1:
                self.goto_index(idx, True)
            else:
                threading.Thread(target=self.find_random_image_dynamic, args=(False,), daemon=True).start()

    def prev_image(self):
        if self.history_pointer > 0:
            self.history_pointer -= 1
            idx = self.history[self.history_pointer]
            self.goto_index(idx, False)

    def load_by_path(self, path):
        # Try to match with existing index if possible
        try:
            idx = self.all_files.index(path)
            self.goto_index(idx)
        except ValueError:
            # File not yet in scan list, add it
            self.all_files.append(path)
            idx = len(self.all_files) - 1
            self.goto_index(idx)

    def nav_sibling(self, direction):
        if not self.current_path: return
        parent = os.path.dirname(self.current_path)
        try:
            files = sorted([os.path.join(parent, f) for f in os.listdir(parent) 
                           if os.path.splitext(f)[1].lower() in CFG_EXTENSIONS])
            if not files: return
            
            try:
                curr_idx = files.index(self.current_path)
                next_idx = (curr_idx + direction) % len(files)
                self.load_by_path(files[next_idx])
            except ValueError:
                if files: self.load_by_path(files[0])
        except OSError:
            pass

    def next_file_alpha(self): self.nav_sibling(1)
    def prev_file_alpha(self): self.nav_sibling(-1)
    def first_file_folder(self):
        if not self.current_path: return
        parent = os.path.dirname(self.current_path)
        try:
            files = sorted([os.path.join(parent, f) for f in os.listdir(parent) 
                           if os.path.splitext(f)[1].lower() in CFG_EXTENSIONS])
            if files: self.load_by_path(files[0])
        except OSError: pass

    # --- Display Logic ---

    def display_current_image(self):
        if not self.current_path: return
        self.image_shown_flag = True
        
        mode = 3 if (self.zoom_mode == 3 or self.temp_zoom) else self.zoom_mode
        if self.temp_zoom and self.zoom_mode != 3: mode = 3 

        self.update_loader_dims()
        
        pil_img, tk_img = self.loader.load_image(self.current_path, mode, self.rotation)
        
        if not pil_img:
            self.canvas.delete("all")
            self.canvas.create_text(self.winfo_width()//2, self.winfo_height()//2, 
                                    text="Error loading image", fill="white")
            return

        self.current_tk_image = tk_img
        self.canvas.delete("all")
        
        cx, cy = self.winfo_width()//2, self.winfo_height()//2
        self.canvas.create_image(cx, cy, image=tk_img, anchor='center', tags='img')
        
        if mode == 3:
             self.update_zoom_pan()
             
        self.update_info_label(pil_img)

    def update_info_label(self, img_obj):
        if not self.current_path or self.info_mode == 3:
            self.lbl_info.config(text="")
            return
            
        try:
            stats = os.stat(self.current_path)
            f_size = Utils.format_size(stats.st_size)
            res = f"{img_obj.width}x{img_obj.height}"
            
            parts = []
            if self.info_mode in [0, 2]:
                parts.append(self.current_path)
            elif self.info_mode == 1:
                parts.append(os.path.basename(self.current_path))
                
            if self.info_mode == 0:
                parts.append(f"[{res}]")
                parts.append(f"[{f_size}]")
            
            self.lbl_info.config(text="  ".join(parts))
        except:
            pass

    def on_resize(self, event):
        if hasattr(self, '_resize_job'):
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(100, self.display_current_image)

    def update_loader_dims(self):
        self.loader.update_screen_size(self.winfo_width(), self.winfo_height())

    # --- Zoom & Pan ---

    def cycle_zoom(self):
        modes = ["ZOOM Fit", "ZOOM Orig", "ZOOM Fill", "ZOOM 4x"]
        self.zoom_mode = (self.zoom_mode + 1) % 4
        self.btn_zoom.config(text=modes[self.zoom_mode])
        self.display_current_image()

    def enable_temp_zoom(self, event):
        if not self.temp_zoom:
            self.temp_zoom = True
            self.display_current_image()

    def disable_temp_zoom(self, event):
        if self.temp_zoom:
            self.temp_zoom = False
            self.display_current_image()

    def on_canvas_motion(self, event):
        if (self.zoom_mode == 3 or self.temp_zoom) and hasattr(self, 'current_tk_image'):
            w, h = self.winfo_width(), self.winfo_height()
            iw, ih = self.current_tk_image.width(), self.current_tk_image.height()
            
            if iw > w:
                ratio_x = event.x / w
                img_left = - (iw - w) * ratio_x
                cx = img_left + iw/2
            else:
                cx = w/2

            if ih > h:
                ratio_y = event.y / h
                img_top = - (ih - h) * ratio_y
                cy = img_top + ih/2
            else:
                cy = h/2
            
            self.canvas.coords('img', cx, cy)

    def update_zoom_pan(self):
        x = self.winfo_pointerx() - self.winfo_rootx()
        y = self.winfo_pointery() - self.winfo_rooty()
        class E: pass
        e = E()
        e.x, e.y = x, y
        self.on_canvas_motion(e)

    def rotate_image(self, deg):
        self.rotation = (self.rotation - deg) % 360
        self.display_current_image()

    # --- UI Interactions ---

    def show_help(self):
        text = """
        KEYBOARD SHORTCUTS
        
        [Navigation]
        Right Arrow : Next Random Image
        Left Arrow  : Previous Image (History)
        Space       : Play / Pause
        Enter       : Open File Location
        
        [Folder Navigation]
        PgDn        : Next File in Folder
        PgUp        : Prev File in Folder
        Home        : First File in Folder
        
        [View]
        Z           : Cycle Zoom (Fit/Orig/Fill/4x)
        Shift (Hold): Temporary 4x Zoom
        Ctrl+R      : Rotate Clockwise
        Ctrl+E      : Rotate Counter-Clockwise
        I           : Cycle File Info modes
        
        [Window]
        F11 / Alt+Enter : Fullscreen
        Esc             : Exit Fullscreen / Quit
        F1              : This Help
        
        [Command Line]
        --cwd           : Start in current directory (default: script dir)
        --fullscreen    : Start in fullscreen mode
        """
        messagebox.showinfo("Help", text)

    def show_context_menu(self, event):
        m = Menu(self, tearoff=0)
        m.add_command(label="Next Random", command=self.next_image)
        m.add_command(label="Play/Pause", command=self.toggle_pause)
        m.add_separator()
        m.add_command(label="Open Folder", command=self.open_current_folder)
        m.add_command(label="Toggle Fullscreen", command=self.toggle_fullscreen)
        m.tk_popup(event.x_root, event.y_root)

    def check_toolbar_hover(self, event):
        if self.toolbar_locked: return
        
        root_y = self.winfo_rooty()
        pointer_y = self.winfo_pointery()
        
        if pointer_y < root_y or pointer_y > root_y + self.winfo_height(): return
        
        rel_y = pointer_y - root_y
        win_h = self.winfo_height()
        
        if win_h - rel_y < CFG_TOOLBAR_TRIGGER_ZONE:
            self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)
        else:
            self.toolbar.place(relx=0, rely=1.0, y=100, anchor='sw', relwidth=1.0)

    def toggle_toolbar_lock(self):
        self.toolbar_locked = not self.toolbar_locked
        self.btn_lock.config(text="HIDE" if self.toolbar_locked else "FIX")
        if self.toolbar_locked:
            self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)

    def toggle_fullscreen(self, force_exit=False):
        if force_exit:
            self.fullscreen = False
        else:
            self.fullscreen = not self.fullscreen
            
        self.attributes("-fullscreen", self.fullscreen)
        
        if self.fullscreen:
            self.w_state_before_full = self.state()
            self.was_locked_before_fs = self.toolbar_locked
            self.toolbar_locked = False
        else:
            self.overrideredirect(False)
            self.toolbar_locked = self.was_locked_before_fs
            
        self.btn_lock.config(text="HIDE" if self.toolbar_locked else "FIX")
        if self.toolbar_locked:
             self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)

    def cycle_info_mode(self):
        self.info_mode = (self.info_mode + 1) % 4
        self.display_current_image()

    def toggle_pause(self):
        self.is_paused = not self.is_paused
        self.btn_play.config(text="PLAY" if self.is_paused else "PAUSE")
        if not self.is_paused:
            self.schedule_next_slide()
        elif self.slide_timer:
            self.after_cancel(self.slide_timer)
            self.slide_timer = None

    def on_speed_change(self, *args):
        val = self.speed_var.get().replace(',', '.')
        try:
            f = float(val)
            if f <= 0: raise ValueError
        except ValueError:
            self.is_paused = True
            self.btn_play.config(text="PLAY")

    def schedule_next_slide(self):
        if self.slide_timer:
            self.after_cancel(self.slide_timer)
        
        if self.is_paused: return
        
        try:
            val = self.speed_var.get().replace(',', '.')
            sec = float(val)
        except ValueError:
            sec = 4.0
            
        self.slide_timer = self.after(int(sec * 1000), self.auto_next)

    def auto_next(self):
        if not self.is_paused:
            self.next_image()
            self.schedule_next_slide()

    def reset_timer(self):
        self.schedule_next_slide()

    def open_current_folder(self):
        if not self.current_path: return
        self.is_paused = True
        self.btn_play.config(text="PLAY")
        
        p = os.path.normpath(self.current_path)
        try:
            if os.name == 'nt':
                subprocess.run(['explorer', '/select,', p])
            else:
                subprocess.run(['xdg-open', os.path.dirname(p)])
        except Exception as e:
            print(e)

if __name__ == "__main__":
    app = SlideShowApp()
    if os.name == 'nt':
        app.state('zoomed')
    else:
        app.attributes('-zoomed', True)
    
    app.mainloop()
