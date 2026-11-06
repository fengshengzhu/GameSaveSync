# -*- coding: utf-8 -*-
"""tkinter 图形界面。

布局采用 ttk.Notebook 分页：主页面只保留「同步与连接」，WebDAV 云备份、
选项与关于分别独立成页，避免主窗口被拉得过长。
"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog

from . import config as cfgmod
from . import tray as traymod
from .engine import SyncEngine


class App:
    def __init__(self, root: tk.Tk):
        # 先迁移旧版遗留在 exe 旁边的配置，再加载
        self._migrate_msgs = cfgmod.migrate_legacy_files()

        self.root = root
        self.cfg = cfgmod.load()
        self.log_q: queue.Queue = queue.Queue()
        self.tray_q: queue.Queue = queue.Queue()
        self.tray = traymod.TrayIcon("游戏存档同步 GameSaveSync", self.tray_q)
        self.engine = SyncEngine(self.cfg, self._enqueue_log)
        self._balloon_shown = False

        root.title(f"游戏存档同步 · GameSaveSync v{cfgmod.VERSION}")
        geometry = self.cfg.window_geometry or "780x640"
        root.geometry(geometry)
        root.minsize(720, 560)

        self._build_ui()
        self._refresh_shares()
        self._refresh_addresses()
        self._poll_log()
        self._poll_tray()
        self._poll_peers()
        self._enqueue_log(f"配置文件：{cfgmod.CONFIG_PATH}")
        for m in self._migrate_msgs:
            self._enqueue_log(m)
        # 关闭按钮 = 最小化到托盘（Windows）；其他平台直接退出
        if traymod.available():
            root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
            self.tray.start()
        else:
            root.protocol("WM_DELETE_WINDOW", self._quit_app)
        # 启动即开监听，便于被对端连接
        self.root.after(300, self.toggle_service)

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # 顶部：服务开关（常驻，不属于任何分页）
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        self.btn_service = ttk.Button(top, text="停止服务", command=self.toggle_service)
        self.btn_service.pack(side="left")
        self.lbl_status = ttk.Label(top, text="启动中…", foreground="#1a7f37")
        self.lbl_status.pack(side="left", padx=12)
        ttk.Label(top, text=f"v{cfgmod.VERSION}", foreground="#999").pack(side="right", padx=4)

        # 分页
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, **pad)
        tab_sync = ttk.Frame(self.nb)
        tab_dav = ttk.Frame(self.nb)
        tab_opt = ttk.Frame(self.nb)
        self.nb.add(tab_sync, text="  同步与连接  ")
        self.nb.add(tab_dav, text="  WebDAV 云备份  ")
        self.nb.add(tab_opt, text="  选项与关于  ")

        self._build_tab_sync(tab_sync)
        self._build_tab_dav(tab_dav)
        self._build_tab_opt(tab_opt)

        # 日志（底部常驻）
        logf = ttk.LabelFrame(self.root, text="日志")
        logf.pack(fill="both", expand=True, **pad)
        self.txt_log = scrolledtext.ScrolledText(logf, height=7, state="disabled",
                                                 font=("Consolas", 9))
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=4)

    def _build_tab_sync(self, tab):
        pad = {"padx": 6, "pady": 4}

        # 设备与配对
        dev = ttk.LabelFrame(tab, text="本机设置（两台电脑配对码必须一致）")
        dev.pack(fill="x", **pad)
        ttk.Label(dev, text="本机名称：").grid(row=0, column=0, sticky="e", padx=6, pady=4)
        self.ent_name = ttk.Entry(dev, width=18)
        self.ent_name.insert(0, self.cfg.device_name)
        self.ent_name.grid(row=0, column=1, padx=4)
        ttk.Label(dev, text="配对码：").grid(row=0, column=2, sticky="e", padx=6)
        self.ent_token = ttk.Entry(dev, width=18)
        self.ent_token.insert(0, self.cfg.token)
        self.ent_token.grid(row=0, column=3, padx=4)
        ttk.Button(dev, text="重新生成", command=self._regen_token).grid(row=0, column=4, padx=4)
        ttk.Label(dev, text="端口：").grid(row=0, column=5, sticky="e", padx=6)
        self.ent_port = ttk.Entry(dev, width=8)
        self.ent_port.insert(0, str(self.cfg.port))
        self.ent_port.grid(row=0, column=6, padx=4)
        ttk.Button(dev, text="保存设置", command=self._save_basic).grid(row=0, column=7, padx=8)

        ttk.Label(dev, text="本机地址（异地时把 IPv6 地址发给另一台电脑手动填写）：").grid(
            row=1, column=0, columnspan=4, sticky="w", padx=6)
        self.lst_addr = tk.Listbox(dev, height=2, activestyle="none")
        self.lst_addr.grid(row=2, column=0, columnspan=8, sticky="we", padx=6, pady=4)
        dev.columnconfigure(1, weight=1)

        # 共享夹
        share = ttk.LabelFrame(tab, text="要同步的文件夹（两端按「名称」配对，名称必须完全一致）")
        share.pack(fill="both", expand=True, **pad)
        cols = ("name", "path")
        self.tree = ttk.Treeview(share, columns=cols, show="headings", height=5)
        self.tree.heading("name", text="共享夹名称")
        self.tree.heading("path", text="本地路径")
        self.tree.column("name", width=160, anchor="w")
        self.tree.column("path", width=520, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6, pady=4)
        bar = ttk.Frame(share)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="添加文件夹…", command=self._add_share).pack(side="left")
        ttk.Button(bar, text="删除选中", command=self._remove_share).pack(side="left", padx=6)

        # 对端连接
        peer = ttk.LabelFrame(tab, text="对端电脑（局域网自动发现；异地填 IPv6）")
        peer.pack(fill="x", **pad)
        self.lst_peers = tk.Listbox(peer, height=3, activestyle="none")
        self.lst_peers.pack(fill="x", padx=6, pady=4)
        prow = ttk.Frame(peer)
        prow.pack(fill="x", padx=6, pady=4)
        ttk.Label(prow, text="地址（IPv4 或 [IPv6]:端口）：").pack(side="left")
        self.ent_target = ttk.Entry(prow)
        self.ent_target.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(prow, text="连接", command=self._connect_manual).pack(side="left")
        self.lst_peers.bind("<Double-Button-1>", lambda e: self._connect_selected())

    def _build_tab_dav(self, tab):
        pad = {"padx": 6, "pady": 4}
        wd = ttk.LabelFrame(tab, text="WebDAV 云盘备份（Cloudreve / 坚果云 / Nextcloud 等）")
        wd.pack(fill="both", expand=True, **pad)

        self.var_webdav = tk.BooleanVar(value=self.cfg.webdav_enabled)
        ttk.Checkbutton(wd, text="启用自动备份（随同步间隔周期比对，只上传有变化的文件）",
                        variable=self.var_webdav).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 2))

        rows = [
            ("服务器地址：", "ent_wd_url", self.cfg.webdav_url, 46, ""),
            ("用户名：", "ent_wd_user", self.cfg.webdav_user, 22, "密码："),
        ]
        ttk.Label(wd, text="服务器地址：").grid(row=1, column=0, sticky="e", padx=8, pady=4)
        self.ent_wd_url = ttk.Entry(wd)
        self.ent_wd_url.insert(0, self.cfg.webdav_url)
        self.ent_wd_url.grid(row=1, column=1, columnspan=3, sticky="we", padx=4, pady=4)

        ttk.Label(wd, text="用户名：").grid(row=2, column=0, sticky="e", padx=8, pady=4)
        self.ent_wd_user = ttk.Entry(wd, width=24)
        self.ent_wd_user.insert(0, self.cfg.webdav_user)
        self.ent_wd_user.grid(row=2, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(wd, text="密码：").grid(row=2, column=2, sticky="e", padx=8)
        self.ent_wd_pass = ttk.Entry(wd, width=24, show="*")
        self.ent_wd_pass.insert(0, self.cfg.webdav_pass)
        self.ent_wd_pass.grid(row=2, column=3, sticky="w", padx=4, pady=4)

        ttk.Label(wd, text="远端目录：").grid(row=3, column=0, sticky="e", padx=8, pady=4)
        self.ent_wd_dir = ttk.Entry(wd, width=24)
        self.ent_wd_dir.insert(0, self.cfg.webdav_dir)
        self.ent_wd_dir.grid(row=3, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(wd, text="备份到 <地址>/<远端目录>/<共享夹名>/…",
                  foreground="#888").grid(row=3, column=2, columnspan=2, sticky="w", padx=8)

        self.var_wd_insecure = tk.BooleanVar(value=self.cfg.webdav_insecure)
        ttk.Checkbutton(wd, text="忽略证书错误", variable=self.var_wd_insecure).grid(
            row=4, column=1, sticky="w", padx=4, pady=2)
        ttk.Label(wd, text="仅用于自签名证书或证书链异常的自建服务器（如 CA 交叉证书过期）：\n"
                           "流量仍然加密，只是不校验服务器身份，请勿用于公共网盘。",
                  foreground="#888", justify="left").grid(
            row=5, column=1, columnspan=3, sticky="w", padx=4)

        btns = ttk.Frame(wd)
        btns.grid(row=6, column=0, columnspan=4, sticky="w", padx=8, pady=(10, 4))
        ttk.Button(btns, text="保存设置", command=self._save_webdav).pack(side="left")
        ttk.Button(btns, text="立即备份", command=self._webdav_backup_now).pack(side="left", padx=8)
        ttk.Button(btns, text="从云端恢复…", command=self._webdav_restore_dialog).pack(side="left")

        ttk.Label(wd, text="Cloudreve：管理面板勾选 WebDAV 权限 → 新建 WebDAV 账户 → 地址填 https://服务器/dav",
                  foreground="#888").grid(
            row=7, column=0, columnspan=4, sticky="w", padx=8, pady=(10, 8))

        wd.columnconfigure(1, weight=1)

    def _build_tab_opt(self, tab):
        pad = {"padx": 6, "pady": 4}

        opt = ttk.LabelFrame(tab, text="同步选项")
        opt.pack(fill="x", **pad)
        self.var_auto = tk.BooleanVar(value=self.cfg.auto_sync)
        ttk.Checkbutton(opt, text="自动同步（连接后按间隔周期比对）", variable=self.var_auto,
                        command=self._save_options).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        ttk.Label(opt, text="间隔（秒）：").grid(row=0, column=2, sticky="e", padx=6)
        self.ent_interval = ttk.Entry(opt, width=6)
        self.ent_interval.insert(0, str(self.cfg.interval))
        self.ent_interval.grid(row=0, column=3, sticky="w", padx=4)
        ttk.Button(opt, text="立即同步一次",
                   command=lambda: threading.Thread(target=self.engine.run_sync_once,
                                                    daemon=True).start()).grid(
            row=0, column=4, padx=10)
        self.var_delete = tk.BooleanVar(value=self.cfg.delete_mirror)
        ttk.Checkbutton(opt, text="删除镜像（对端删除也删，谨慎）", variable=self.var_delete,
                        command=self._save_options).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        ttk.Label(opt, text="默认只新增/覆盖，不删除文件，避免误删存档；WebDAV 备份的删除联动也随此选项。",
                  foreground="#888").grid(row=1, column=2, columnspan=3, sticky="w", padx=6)

        dataf = ttk.LabelFrame(tab, text="数据存放")
        dataf.pack(fill="x", **pad)
        ttk.Label(dataf, text=f"配置与备份状态保存在：\n{cfgmod.APP_DIR}",
                  foreground="#555", justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        ttk.Button(dataf, text="打开此文件夹", command=self._open_config_folder).grid(
            row=1, column=0, sticky="w", padx=8, pady=(0, 6))
        ttk.Label(dataf, text="程序本身不产生任何文件；把 exe 放到桌面、下载等任意位置都可以。",
                  foreground="#888").grid(row=1, column=1, sticky="w", padx=6, pady=(0, 6))

        about = ttk.LabelFrame(tab, text="关于")
        about.pack(fill="both", expand=True, **pad)
        ttk.Label(about, justify="left", foreground="#555", text=(
            f"GameSaveSync v{cfgmod.VERSION}\n"
            "两台电脑间免安装同步游戏存档（局域网 / 公网 IPv6 直连），\n"
            "并支持把存档自动备份到 WebDAV 网盘（Cloudreve、坚果云、Nextcloud 等）。\n\n"
            "开源地址：github.com/fengshengzhu/GameSaveSync\n"
            "问题反馈：github.com/fengshengzhu/GameSaveSync/issues\n\n"
            "提示：关闭窗口只是最小化到系统托盘，同步在后台继续；\n"
            "右键托盘图标 →「退出」才真正结束程序。")).pack(
            anchor="w", padx=10, pady=8)

    # ---------------- 事件 ----------------
    def toggle_service(self):
        if self.engine.running:
            self.engine.stop()
            self.btn_service.config(text="启动服务")
            self.lbl_status.config(text="已停止", foreground="#b3261e")
        else:
            try:
                self._save_basic(silent=True)
                self.engine.start()
                self.btn_service.config(text="停止服务")
                self.lbl_status.config(text="监听中", foreground="#1a7f37")
            except OSError as e:
                messagebox.showerror("启动失败", f"端口 {self.cfg.port} 无法绑定：\n{e}\n可换一个端口再试。")

    def _regen_token(self):
        self.ent_token.delete(0, "end")
        self.ent_token.insert(0, cfgmod.generate_token())
        self._save_basic(silent=True)

    def _save_basic(self, silent=False):
        try:
            self.cfg.device_name = self.ent_name.get()
            self.cfg.token = self.ent_token.get()
            self.cfg.port = int(self.ent_port.get())
            self.cfg.save()
            if not silent:
                messagebox.showinfo("已保存", "本机设置已保存。")
        except ValueError:
            messagebox.showerror("错误", "端口必须是 1024-65535 的数字。")

    def _save_options(self):
        self.cfg.auto_sync = self.var_auto.get()
        self.cfg.delete_mirror = self.var_delete.get()
        try:
            self.cfg.interval = int(self.ent_interval.get())
        except ValueError:
            pass
        self.cfg.save()

    def _open_config_folder(self):
        path = cfgmod.APP_DIR
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')
        except OSError as e:
            messagebox.showerror("打开失败", str(e))

    def _save_webdav_fields(self):
        """把界面上的 WebDAV 输入写回配置并落盘。"""
        self.cfg.webdav_enabled = self.var_webdav.get()
        self.cfg.webdav_url = self.ent_wd_url.get()
        self.cfg.webdav_user = self.ent_wd_user.get()
        self.cfg.webdav_pass = self.ent_wd_pass.get()
        self.cfg.webdav_insecure = self.var_wd_insecure.get()
        self.cfg.webdav_dir = self.ent_wd_dir.get()
        self.cfg.save()

    def _save_webdav(self):
        self._save_webdav_fields()
        messagebox.showinfo("已保存", "WebDAV 设置已保存。\n"
                            "若服务正在运行，备份将按同步间隔自动进行；"
                            "也可点「立即备份」马上上传一次。")

    def _webdav_backup_now(self):
        # 先把当前输入保存下来再备份，避免「改了没保存就备份」的困惑
        self._save_webdav_fields()
        threading.Thread(target=self.engine.webdav.run_once,
                         kwargs={"manual": True}, daemon=True).start()

    def _webdav_restore_dialog(self):
        self._save_webdav_fields()
        if not self.cfg.webdav_url or not self.cfg.webdav_user:
            messagebox.showwarning("无法恢复", "请先填写 WebDAV 服务器地址和用户名。")
            return
        if not self.cfg.shares:
            messagebox.showwarning("无法恢复", "请先在「同步与连接」页添加共享夹。")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("从云端恢复")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="选择共享夹：").grid(row=0, column=0, sticky="w")
        names = [s["name"] for s in self.cfg.shares]
        cb = ttk.Combobox(frm, values=names, state="readonly", width=26)
        cb.current(0)
        cb.grid(row=0, column=1, sticky="w", padx=4, pady=4)

        mode = tk.StringVar(value="missing")
        ttk.Radiobutton(frm, text="仅补齐本地缺失的文件（更安全，本地已有的不动）",
                        variable=mode, value="missing").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Radiobutton(frm, text="云端覆盖本地所有文件（以云端版本为准）",
                        variable=mode, value="overwrite").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(frm, text="恢复会从云端下载文件到本地共享夹，并按云端时间还原修改时间；\n"
                            "下载完成后会自动更新备份状态，不会被下一轮备份重复上传。",
                  foreground="#888", justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 4))

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(8, 0))

        def do_restore():
            share, m = cb.get(), mode.get()
            dlg.destroy()

            def work():
                self.engine.log(f"[WebDAV] 开始从云端恢复 {share}（{'覆盖' if m == 'overwrite' else '补齐'}模式）…")
                try:
                    self.engine.webdav.restore(share, m)
                except Exception as e:
                    self.engine.log(f"[WebDAV] 恢复失败：{type(e).__name__}: {e}")
            threading.Thread(target=work, daemon=True).start()

        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="left", padx=4)
        ttk.Button(btns, text="开始恢复", command=do_restore).pack(side="left")

    def _refresh_addresses(self):
        self.lst_addr.delete(0, "end")
        for ip in cfgmod.local_addresses():
            self.lst_addr.insert("end", ip)

    def _refresh_shares(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for s in self.cfg.shares:
            self.tree.insert("", "end", values=(s["name"], s["path"]))

    def _add_share(self):
        name = simpledialog.askstring("共享夹名称", "给这个存档文件夹起个名字（两端要一致）：", parent=self.root)
        if not name:
            return
        path = filedialog.askdirectory(title=f"选择「{name}」对应的本地存档文件夹", parent=self.root)
        if not path:
            return
        self.cfg.add_share(name.strip(), path)
        self.cfg.save()
        self._refresh_shares()

    def _remove_share(self):
        sel = self.tree.selection()
        if not sel:
            return
        name = self.tree.item(sel[0], "values")[0]
        self.cfg.remove_share(name)
        self.cfg.save()
        self._refresh_shares()

    def _connect_manual(self):
        target = self.ent_target.get().strip()
        if not target:
            return
        # 连接含握手 + 首轮同步，放后台线程避免卡界面
        threading.Thread(target=self.engine.connect, args=(target,), daemon=True).start()

    def _connect_selected(self):
        sel = self.lst_peers.curselection()
        if not sel:
            return
        item = self.lst_peers.get(sel[0])
        # 列表项格式：名称  [v4/v6]  addr:port
        addr = item.split()[-1]
        threading.Thread(target=self.engine.connect, args=(addr,), daemon=True).start()

    # ---------------- 窗口尺寸 ----------------
    def _remember_geometry(self):
        g = self.root.winfo_geometry()
        if "x" in g:
            self.cfg.window_geometry = g.split("+")[0]
            self.cfg.save()

    # ---------------- 托盘与退出 ----------------
    def _hide_to_tray(self):
        self._remember_geometry()
        self.root.withdraw()
        if not self._balloon_shown:
            self._balloon_shown = True
            self.tray.show_balloon("游戏存档同步",
                                   "程序已最小化到系统托盘，同步仍在后台进行。\n"
                                   "单击托盘图标恢复窗口，右键可退出。")

    def _poll_tray(self):
        while True:
            try:
                evt = self.tray_q.get_nowait()
            except queue.Empty:
                break
            if evt == traymod.EVT_RESTORE:
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            elif evt == traymod.EVT_QUIT:
                self._quit_app()
                return
        self.root.after(300, self._poll_tray)

    def _quit_app(self):
        self._remember_geometry()
        self.engine.stop()
        self.cfg.save()
        self.tray.stop()
        self.root.destroy()

    # ---------------- 轮询 ----------------
    def _enqueue_log(self, msg: str):
        self.log_q.put(msg)

    def _poll_log(self):
        while True:
            try:
                msg = self.log_q.get_nowait()
            except queue.Empty:
                break
            stamp = time.strftime("%H:%M:%S")
            self.txt_log.config(state="normal")
            self.txt_log.insert("end", f"[{stamp}] {msg}\n")
            self.txt_log.see("end")
            self.txt_log.config(state="disabled")
        self.root.after(300, self._poll_log)

    def _poll_peers(self):
        self.lst_peers.delete(0, "end")
        for p in self.engine.discovery.snapshot():
            tag = "v6" if p["v6"] else "v4"
            self.lst_peers.insert("end", f"{p['name']}  [{tag}]  {p['addr']}:{p['port']}")
        self.root.after(3000, self._poll_peers)


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
