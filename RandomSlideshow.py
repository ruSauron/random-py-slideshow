import os
import sys
import time
import random
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, Menu
from collections import deque
from pathlib import Path

# Попытка импорта Pillow
try:
    from PIL import Image, ImageTk, ImageOps
except ImportError:
    messagebox.showerror("Ошибка", "Библиотека Pillow не установлена.\npip install Pillow")
    sys.exit(1)

# =================================================================================
# НАСТРОЙКИ (КОНФИГУРАЦИЯ)
# =================================================================================

CFG_SLIDE_DURATION = 4.0
CFG_EXTENSIONS = {'.bmp', '.gif', '.jpg', '.jpeg', '.jfif', '.png', '.webp', '.ico', '.tiff'}
CFG_BG_COLOR = "#000000"
CFG_TEXT_COLOR = "#FFFFFF"
CFG_FONT = ("Segoe UI", 10)
CFG_TOOLBAR_TRIGGER_ZONE = 100  # Пиксели от низа окна
CFG_TOOLBAR_HEIGHT = 40
CFG_CACHE_SIZE = 5

# =================================================================================

class Utils:
    @staticmethod
    def get_default(var_name, default_val):
        return globals().get(var_name, default_val)

    @staticmethod
    def format_size(size_bytes):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.2f} TB"

class ImageLoader:
    def __init__(self):
        self.cache = {} 
        self.lock = threading.Lock()
        self.current_screen_size = (1920, 1080)
    
    def update_screen_size(self, width, height):
        self.current_screen_size = (width, height)

    def load_image(self, path, fit_mode, rotation, force_reload=False):
        cache_key = (path, fit_mode, rotation, self.current_screen_size)
        with self.lock:
            if not force_reload and cache_key in self.cache:
                return self.cache[cache_key]

        try:
            safe_path = str(Path(path).resolve())
            if os.name == 'nt' and not safe_path.startswith('\\\\?\\'):
                 safe_path = '\\\\?\\' + safe_path

            img = Image.open(safe_path)
            img = ImageOps.exif_transpose(img)
            
            if rotation != 0:
                img = img.rotate(rotation, expand=True)

            sw, sh = self.current_screen_size
            iw, ih = img.size
            
            target_w, target_h = iw, ih

            # 0=Fit, 1=Original, 2=Fill
            if fit_mode == 0: # Fit
                ratio = min(sw/iw, sh/ih)
                target_w, target_h = int(iw * ratio), int(ih * ratio)
                if target_w < 1: target_w = 1
                if target_h < 1: target_h = 1
                img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
            
            elif fit_mode == 1: # Original
                pass 
                
#            elif fit_mode == 2: # 4x Zoom (логика отрисовки в Canvas, здесь грузим оригинал)
#                pass 
            
            elif fit_mode == 2: # Fill
                ratio = max(sw/iw, sh/ih)
                target_w, target_h = int(iw * ratio), int(ih * ratio)
                img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

            tk_img = ImageTk.PhotoImage(img)
            
            with self.lock:
                if len(self.cache) > Utils.get_default('CFG_CACHE_SIZE', 5):
                    self.cache.pop(next(iter(self.cache)))
                self.cache[cache_key] = (img, tk_img)
                
            return img, tk_img
        except Exception as e:
            print(f"Error loading {path}: {e}")
            return None, None

class SlideShowApp(tk.Tk):
    def __init__(self):
        super().__init__()
        
        self.title("Fast Random PySlideshow")
        self.geometry("1024x768")
        self.configure(bg=Utils.get_default('CFG_BG_COLOR', '#000'))
        
        # Состояние
        self.all_files = []
        self.unviewed_indices = []
        self.history = deque(maxlen=500)
        self.history_pointer = -1
        
        self.current_file_index = -1
        self.current_path = None
        self.is_paused = False
        self.slide_timer = None
        
        # Настройки View
        self.zoom_mode = 0 
        self.temp_zoom = False
        self.rotation = 0
        self.info_mode = 0
        
        # Toolbar state
        self.toolbar_locked = True  # По умолчанию закреплена
        self.was_locked_before_fullscreen = True
        self.fullscreen = False
        self.w_state_before_full = 'zoomed'
        
        self.loader = ImageLoader()
        
        self.setup_ui()
        self.bind_events()
        self.start_folder_scan()

    def setup_ui(self):
        # Канвас
        self.canvas = tk.Canvas(self, bg=Utils.get_default('CFG_BG_COLOR', '#000'), highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        
        # Панель
        self.toolbar = tk.Frame(self, bg='#333333', height=Utils.get_default('CFG_TOOLBAR_HEIGHT', 40))
        self.toolbar.pack_propagate(False)
        # Ставим вниз
        self.toolbar.place(relx=0, rely=1.0, y=0 if self.toolbar_locked else 100, anchor='sw', relwidth=1.0)
        
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TButton', font=('Segoe UI', 9), padding=2)
        
        # === КНОПКИ ===
        
        # ? Help
        ttk.Button(self.toolbar, text="?", width=2, command=self.show_help).pack(side='left', padx=2)

        # LOCK
        self.btn_lock = ttk.Button(self.toolbar, text="SHOW" if not self.toolbar_locked else "HIDE", width=6, command=self.toggle_toolbar_lock)
        self.btn_lock.pack(side='left', padx=2)
        
        # FULL
        self.btn_full = ttk.Button(self.toolbar, text="FULL", width=5, command=self.toggle_fullscreen)
        self.btn_full.pack(side='left', padx=2)
        
        # ZOOM
        self.btn_zoom = ttk.Button(self.toolbar, text="ZOOM: Fit", width=10, command=self.cycle_zoom)
        self.btn_zoom.pack(side='left', padx=2)
        
        # ROTATE (Исправлено: меняем -90 и 90 местами по просьбе пользователя)
        # CCR (Counter Clockwise)
        ttk.Button(self.toolbar, text="↺", width=3, command=lambda: self.rotate_image(-90)).pack(side='left', padx=2)
        # CR (Clockwise)
        ttk.Button(self.toolbar, text="↻", width=3, command=lambda: self.rotate_image(90)).pack(side='left', padx=2)
        
        # INFO LABEL
        self.lbl_info = tk.Label(self.toolbar, text="", bg='#333333', fg='white', font=('Segoe UI', 9), anchor='e')
        self.lbl_info.pack(side='right', padx=10, fill='x', expand=True)
        self.lbl_info.bind("<Button-1>", lambda e: self.cycle_info_mode())
        # Контекстное меню лейбла оставим для переключения инфо
        
        # FOLDER
        ttk.Button(self.toolbar, text="FOLDER", width=7, command=self.open_current_folder).pack(side='right', padx=2)

        # SPEED
        self.speed_var = tk.StringVar(value=str(Utils.get_default('CFG_SLIDE_DURATION', 4.0)))
        self.speed_var.trace("w", self.on_speed_change)
        self.ent_speed = ttk.Entry(self.toolbar, textvariable=self.speed_var, width=5, justify='center')
        self.ent_speed.pack(side='right', padx=2)
        tk.Label(self.toolbar, text="sec", bg='#333333', fg='white').pack(side='right')

        # PLAY/PAUSE
        self.btn_play = ttk.Button(self.toolbar, text="⏸PAUSE", width=9, command=self.toggle_pause)
        self.btn_play.pack(side='right', padx=5)

        # NAV Main
        ttk.Button(self.toolbar, text="-->", width=4, command=self.next_image).pack(side='right', padx=2)
        ttk.Button(self.toolbar, text="<--", width=4, command=self.prev_image).pack(side='right', padx=2)
        
        # NAV Folder
        ttk.Button(self.toolbar, text=">", width=3, command=self.next_file_alpha).pack(side='right', padx=2)
        ttk.Button(self.toolbar, text="<", width=3, command=self.prev_file_alpha).pack(side='right', padx=2)
        ttk.Button(self.toolbar, text="|<", width=3, command=self.first_file_folder).pack(side='right', padx=2)

    def bind_events(self):
        # Движение мыши (глобальное для Canvas и Root)
        self.bind('<Motion>', self.check_toolbar_hover)
        
        # Правый клик (Контекстное меню)
        self.canvas.bind('<Button-3>', self.show_context_menu)
        
        # Клавиатура
        self.bind('<Escape>', lambda e: self.toggle_fullscreen(force_exit=True))
        self.bind('<F11>', lambda e: self.toggle_fullscreen())
        self.bind('<Alt-Return>', lambda e: self.toggle_fullscreen())
        self.bind('<F1>', lambda e: self.show_help())
        
        self.bind('<z>', lambda e: self.cycle_zoom())
        self.bind('<Z>', lambda e: self.cycle_zoom())
        self.bind('<Shift_L>', self.enable_temp_zoom)
        self.bind('<KeyRelease-Shift_L>', self.disable_temp_zoom)
        
        # Исправлено вращение по Ctrl
        self.bind('<Control-e>', lambda e: self.rotate_image(-90))
        self.bind('<Control-r>', lambda e: self.rotate_image(90))
        
        self.bind('<i>', lambda e: self.cycle_info_mode())
        
        self.bind('<Left>', lambda e: self.prev_image())
        self.bind('<Right>', lambda e: self.next_image())
        
        self.bind('<Home>', lambda e: self.first_file_folder())
        self.bind('<Prior>', lambda e: self.prev_file_alpha()) 
        self.bind('<Next>', lambda e: self.next_file_alpha()) 
        
        self.bind('<space>', lambda e: self.toggle_pause())
        self.bind('<Return>', lambda e: self.open_current_folder())

        self.canvas.bind('<Motion>', self.on_canvas_motion)
        self.bind('<Configure>', self.on_resize)

    def show_help(self):
        help_text = """
        Горячие клавиши:
        
        F1        : Эта справка
        Alt+Enter : На весь экран (также F11)
        Esc       : Выход из полного экрана
        Пробел    : Пауза / Старт слайд-шоу
        
        Стрелки < > : Пред/След случайная картинка
        PgUp / PgDn : Пред/След файл в текущей папке (по алфавиту)
        Home        : Первый файл в папке
        
        Z           : Смена режима ZOOM (Fit, Orig, 4x, Fill)
        Shift (hold): Временная лупа (4x)
        
        Ctrl+R / Ctrl+E : Вращение по/против часовой
        I           : Инфо о файле
        Enter       : Открыть папку файла
        """
        messagebox.showinfo("Справка", help_text)

    def show_context_menu(self, event):
        m = Menu(self, tearoff=0)
        m.add_command(label="Next (Random)", command=self.next_image)
        m.add_command(label="Prev (History)", command=self.prev_image)
        m.add_separator()
        m.add_command(label="Play/Pause", command=self.toggle_pause)
        m.add_command(label="Zoom Mode (Z)", command=self.cycle_zoom)
        m.add_command(label="Rotate CW", command=lambda: self.rotate_image(90))
        m.add_separator()
        m.add_command(label="Open Folder", command=self.open_current_folder)
        m.add_command(label="Fullscreen", command=self.toggle_fullscreen)
        m.add_command(label="Toggle Toolbar", command=self.toggle_toolbar_lock)
        m.add_separator()
        m.add_command(label="Exit", command=self.quit)
        m.tk_popup(event.x_root, event.y_root)

    # ================= ЛОГИКА СКАНИРОВАНИЯ =================
    def start_folder_scan(self):
        path_arg = "."
        if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
            path_arg = sys.argv[1]
        self.root_dir = os.path.abspath(path_arg)
        t = threading.Thread(target=self.scan_worker, daemon=True)
        t.start()
        self.schedule_next_slide()

    def scan_worker(self):
        exts = Utils.get_default('CFG_EXTENSIONS', set())
        first_batch_found = False
        for root, dirs, files in os.walk(self.root_dir):
            batch = []
            for f in files:
                if os.path.splitext(f)[1].lower() in exts:
                    batch.append(os.path.join(root, f))
            if batch:
                start_idx = len(self.all_files)
                self.all_files.extend(batch)
                self.unviewed_indices.extend(range(start_idx, start_idx + len(batch)))
                if not first_batch_found:
                    first_batch_found = True
                    self.after(0, self.initial_start)
            time.sleep(0.01)

    def initial_start(self):
        if self.all_files: self.next_image()

    # ================= НАВИГАЦИЯ =================
    def get_random_index(self):
        if not self.all_files: return -1
        if not self.unviewed_indices:
            self.unviewed_indices = list(range(len(self.all_files)))
        return random.choice(self.unviewed_indices)

    def go_to_index(self, index, record_history=True):
        if index < 0 or index >= len(self.all_files): return
        path = self.all_files[index]
        self.current_file_index = index
        self.current_path = path
        if index in self.unviewed_indices:
            self.unviewed_indices.remove(index)
        if record_history:
            if self.history_pointer < len(self.history) - 1:
                # Ветвление истории
                pass
            self.history.append(index)
            self.history_pointer = len(self.history) - 1
        self.rotation = 0
        self.display_current_image()
        self.reset_timer()

    def next_image(self):
        if self.history_pointer < len(self.history) - 1:
            self.history_pointer += 1
            self.go_to_index(self.history[self.history_pointer], False)
        else:
            idx = self.get_random_index()
            if idx != -1: self.go_to_index(idx)

    def prev_image(self):
        if self.history_pointer > 0:
            self.history_pointer -= 1
            self.go_to_index(self.history[self.history_pointer], False)

    def next_file_alpha(self):
        self.nav_sibling(1)

    def prev_file_alpha(self):
        self.nav_sibling(-1)

    def nav_sibling(self, direction):
        if not self.current_path: return
        siblings = self.get_siblings(self.current_path)
        if not siblings: return
        try:
            curr = siblings.index(self.current_path)
            nxt = (curr + direction) % len(siblings)
            self.load_by_path(siblings[nxt])
        except ValueError: pass

    def first_file_folder(self):
        if not self.current_path: return
        siblings = self.get_siblings(self.current_path)
        if siblings: self.load_by_path(siblings[0])

    def load_by_path(self, path):
        try:
            idx = self.all_files.index(path)
            self.go_to_index(idx)
        except ValueError: pass

    def get_siblings(self, path):
        parent = os.path.dirname(path)
        exts = Utils.get_default('CFG_EXTENSIONS', set())
        try:
            return sorted([os.path.join(parent, f) for f in os.listdir(parent) if os.path.splitext(f)[1].lower() in exts])
        except OSError: return []

    # ================= ОТОБРАЖЕНИЕ =================
    def display_current_image(self):
        if not self.current_path: return
        self.update_loader_dims()
        mode = 2 if self.temp_zoom else self.zoom_mode
        pil_img, tk_img = self.loader.load_image(self.current_path, mode, self.rotation)
        
        if not pil_img:
            self.canvas.delete("all")
            self.canvas.create_text(self.winfo_width()//2, self.winfo_height()//2, text="Ошибка", fill='white')
            return

        self.current_tk_image = tk_img
        self.current_pil_image = pil_img
        self.canvas.delete("all")
        cx, cy = self.winfo_width()//2, self.winfo_height()//2
        
        self.canvas.create_image(cx, cy, image=tk_img, anchor='center', tags="img")
        if mode == 2: self.update_zoom_pan()
        self.update_info_label(pil_img)

    def on_resize(self, event):
        if hasattr(self, 'resize_job'): self.after_cancel(self.resize_job)
        self.resize_job = self.after(100, self.display_current_image)

    def update_loader_dims(self):
        self.loader.update_screen_size(self.winfo_width(), self.winfo_height())

    def update_info_label(self, img_obj):
        if not self.current_path: return
        text = ""
        if self.info_mode != 3:
            try:
                stats = os.stat(self.current_path)
                fsize = Utils.format_size(stats.st_size)
                res = f"{img_obj.width}x{img_obj.height}"
                parts = []
                if self.info_mode in [0, 2]: parts.append(self.current_path)
                elif self.info_mode == 1: parts.append(os.path.basename(self.current_path))
                if self.info_mode == 0: parts.append(f"| {res} | {fsize}")
                text = " ".join(parts)
            except: pass
        self.lbl_info.config(text=text)

    # ================= ZOOM & ROTATE =================
    def cycle_zoom(self):
        modes = ["Fit", "Orig", "Fill"]
        self.zoom_mode = (self.zoom_mode + 1) % 3
        self.btn_zoom.config(text=f"ZOOM: {modes[self.zoom_mode]}")
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
        if (self.zoom_mode == 2 or self.temp_zoom) and hasattr(self, 'current_tk_image'):
            w, h = self.winfo_width(), self.winfo_height()
            iw, ih = self.current_tk_image.width(), self.current_tk_image.height()
            if iw > w:
                target_x = (w - iw) * (event.x / w)
                dx = target_x + iw/2
                self.canvas.coords("img", dx, self.canvas.coords("img")[1])
            if ih > h:
                target_y = (h - ih) * (event.y / h)
                dy = target_y + ih/2
                self.canvas.coords("img", self.canvas.coords("img")[0], dy)

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

    # ================= UI CONTROL =================
    def check_toolbar_hover(self, event):
        if self.toolbar_locked: return
        
        # Исправленная логика с использованием глобальных координат
        pointer_y = self.winfo_pointery()
        root_y = self.winfo_rooty()
        
        # Защита от ошибок, если окно еще не инициализировано
        if root_y is None: return

        # Y координата мыши относительно верха окна приложения
        rel_y = pointer_y - root_y
        win_height = self.winfo_height()
        
        trigger = Utils.get_default('CFG_TOOLBAR_TRIGGER_ZONE', 100)
        
        if rel_y > win_height - trigger:
            self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)
        else:
            self.toolbar.place(relx=0, rely=1.0, y=100, anchor='sw', relwidth=1.0)

    def toggle_toolbar_lock(self):
        self.toolbar_locked = not self.toolbar_locked
        self.update_lock_btn_text()
        if self.toolbar_locked:
            self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)

    def update_lock_btn_text(self):
        self.btn_lock.config(text="HIDE" if self.toolbar_locked else "SHOW")

    def toggle_fullscreen(self, force_exit=False):
        if force_exit:
            self.fullscreen = False
        else:
            self.fullscreen = not self.fullscreen
            
        self.attributes("-fullscreen", self.fullscreen)
        
        if self.fullscreen:
            # Вход в Fullscreen
            self.w_state_before_full = self.state()
            # Запоминаем, была ли панель закреплена
            self.was_locked_before_fullscreen = self.toolbar_locked
            # Принудительно открепляем и прячем
            self.toolbar_locked = False
            self.update_lock_btn_text()
            # Убираем панель за экран
            self.toolbar.place(relx=0, rely=1.0, y=100, anchor='sw', relwidth=1.0)
        else:
            # Выход из Fullscreen
            self.overrideredirect(False)
            self.state(self.w_state_before_full)
            # Восстанавливаем состояние панели
            self.toolbar_locked = self.was_locked_before_fullscreen
            self.update_lock_btn_text()
            if self.toolbar_locked:
                self.toolbar.place(relx=0, rely=1.0, y=0, anchor='sw', relwidth=1.0)

    def cycle_info_mode(self):
        self.info_mode = (self.info_mode + 1) % 4
        self.display_current_image()

    def toggle_pause(self):
        self.is_paused = not self.is_paused
        self.btn_play.config(text="▶PLAY" if self.is_paused else "⏸PAUSE")
        if not self.is_paused:
            self.schedule_next_slide()
        else:
            if self.slide_timer:
                self.after_cancel(self.slide_timer)
                self.slide_timer = None

    def on_speed_change(self, *args):
        val = self.speed_var.get().replace(',', '.')
        try:
            f = float(val)
            if f > 0:
                self.is_paused = True
                self.btn_play.config(text="▶PLAY")
                if self.slide_timer: self.after_cancel(self.slide_timer)
        except ValueError: pass

    def schedule_next_slide(self):
        if self.slide_timer: self.after_cancel(self.slide_timer)
        if self.is_paused: return
        try:
            val = self.speed_var.get().replace(',', '.')
            sec = float(val)
        except ValueError: sec = 4.0
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
        self.btn_play.config(text="▶PLAY")
        path = os.path.normpath(self.current_path)
        if os.name == 'nt':
            subprocess.run(['explorer', '/select,', path])
        else:
            subprocess.run(['xdg-open', os.path.dirname(path)])

if __name__ == "__main__":
    app = SlideShowApp()
    try: app.state('zoomed')
    except: app.attributes('-fullscreen', True)
    app.mainloop()
