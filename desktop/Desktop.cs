using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: AssemblyVersion("0.1.0.0")]
[assembly: AssemblyFileVersion("0.1.0.0")]
[assembly: AssemblyInformationalVersion("0.1")]

namespace MossImageDesktop
{
    internal sealed class Settings
    {
        public int schema_version { get; set; }
        public string data_dir { get; set; }
        public string model_dir { get; set; }
        public string output_dir { get; set; }
        public string source { get; set; }
    }

    internal static class AppFiles
    {
        internal const string Version = "0.1";
        internal const string ResourceName = "MossPayload.zip";
        internal static readonly string Home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "MOSS-Image-Desktop");
        internal static readonly string ConfigPath = Path.Combine(Home, "settings.json");
        internal static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

        internal static Settings Defaults()
        {
            string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "MOSS-Image-Data");
            string system = Path.GetPathRoot(Environment.SystemDirectory);
            foreach (DriveInfo drive in DriveInfo.GetDrives())
            {
                try
                {
                    if (drive.DriveType == DriveType.Fixed && drive.IsReady && !String.Equals(drive.RootDirectory.FullName, system, StringComparison.OrdinalIgnoreCase)
                        && drive.AvailableFreeSpace >= 60L * 1024 * 1024 * 1024)
                    { root = Path.Combine(drive.RootDirectory.FullName, "MOSS-Image-Data"); break; }
                }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
            return new Settings { schema_version = 2, data_dir = Path.Combine(root, "data"), model_dir = Path.Combine(root, "models"), output_dir = Path.Combine(root, "pictures"), source = "modelscope" };
        }

        internal static Settings ReadSettings(out string notice)
        {
            notice = null;
            if (!File.Exists(ConfigPath)) return Defaults();
            try
            {
                Settings result = Json.Deserialize<Settings>(File.ReadAllText(ConfigPath, Encoding.UTF8));
                ValidateSettings(result, false);
                return result;
            }
            catch (Exception)
            {
                notice = "上次的目录设置无法读取，已显示建议位置。请确认后再启动。原文件未被删除。";
                return Defaults();
            }
        }

        internal static string NormalizeDirectory(string value)
        {
            if (String.IsNullOrWhiteSpace(value)) throw new ArgumentException("请选择一个文件夹。");
            value = value.Trim();
            if (value.Length < 3 || !Char.IsLetter(value[0]) || value[1] != ':' || (value[2] != '\\' && value[2] != '/'))
                throw new ArgumentException("请选择本机磁盘上的绝对路径，例如 D:\\MOSS-Image-Data\\models。");
            if (value.IndexOfAny(new char[] { '"', '<', '>', '|', '?', '*' }) >= 0 || value.IndexOf(':', 2) >= 0 || value.IndexOfAny(new char[] { '\r', '\n', '\0' }) >= 0)
                throw new ArgumentException("文件夹路径含有无效字符。");
            string full = Path.GetFullPath(value).TrimEnd('\\', '/');
            if (full.Length <= 2) throw new ArgumentException("请新建专用文件夹，不要直接选择整个磁盘。");
            string[] protectedDirectories = new string[] {
                Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), Home
            };
            foreach (string protectedDir in protectedDirectories)
                if (!String.IsNullOrEmpty(protectedDir) && Overlaps(full, protectedDir))
                    throw new ArgumentException("请避开 Windows、Program Files、ProgramData 和软件自身目录，选择专用数据文件夹。");
            foreach (string segment in full.Substring(3).Split('\\', '/'))
            {
                string name = segment.Split('.')[0].ToUpperInvariant();
                if (segment.EndsWith(".", StringComparison.Ordinal) || segment.EndsWith(" ", StringComparison.Ordinal)
                    || name == "CON" || name == "PRN" || name == "AUX" || name == "NUL"
                    || (name.Length == 4 && (name.StartsWith("COM", StringComparison.Ordinal) || name.StartsWith("LPT", StringComparison.Ordinal)) && name[3] >= '1' && name[3] <= '9'))
                    throw new ArgumentException("文件夹名称包含 Windows 不支持的名称或结尾。");
            }
            return full;
        }

        internal static bool Overlaps(string a, string b)
        {
            a = Path.GetFullPath(a).TrimEnd('\\', '/');
            b = Path.GetFullPath(b).TrimEnd('\\', '/');
            return String.Equals(a, b, StringComparison.OrdinalIgnoreCase) || a.StartsWith(b + "\\", StringComparison.OrdinalIgnoreCase) || b.StartsWith(a + "\\", StringComparison.OrdinalIgnoreCase);
        }

        internal static void ValidateSettings(Settings settings, bool writable)
        {
            if (settings == null || settings.schema_version != 2) throw new ArgumentException("目录设置格式无效。");
            settings.model_dir = NormalizeDirectory(settings.model_dir);
            settings.data_dir = NormalizeDirectory(settings.data_dir);
            settings.output_dir = NormalizeDirectory(settings.output_dir);
            if (settings.source != "modelscope" && settings.source != "huggingface") throw new ArgumentException("请选择有效的下载源。");
            string[] paths = new string[] { settings.model_dir, settings.data_dir, settings.output_dir };
            for (int i = 0; i < paths.Length; i++)
                for (int j = i + 1; j < paths.Length; j++)
                    if (Overlaps(paths[i], paths[j])) throw new ArgumentException("三个目录应彼此独立，不能相同，也不能互相包含。可以放在同一磁盘的三个并列文件夹中。");
            foreach (string path in paths)
            {
                CheckNoReparse(path);
            }
            // Check derived locations before writing even the first desktop log.
            foreach (string name in new string[] { "runtime", "cache", "logs", "temp" }) CheckNoReparse(Path.Combine(settings.data_dir, name));
            CheckNoReparse(Path.Combine(settings.model_dir, "MOSS-Image-SFT"));
            foreach (string path in paths)
            {
                if (writable)
                {
                    Directory.CreateDirectory(path);
                    string probe = Path.Combine(path, ".moss-write-test-" + Guid.NewGuid().ToString("N"));
                    using (FileStream file = new FileStream(probe, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1, FileOptions.DeleteOnClose)) { file.WriteByte(0); }
                }
            }
        }

        private static void CheckNoReparse(string path)
        {
            // Junctions and symlinks would make free-space and overlap checks misleading.
            DirectoryInfo existing = new DirectoryInfo(path);
            while (existing != null)
            {
                if (existing.Exists && (existing.Attributes & FileAttributes.ReparsePoint) != 0)
                    throw new ArgumentException("请选择真实文件夹，不要使用符号链接或目录联接：" + path);
                existing = existing.Parent;
            }
        }

        internal static void SaveSettings(Settings settings, string target)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(target));
            string temp = target + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                File.WriteAllText(temp, Json.Serialize(settings), new UTF8Encoding(false));
                if (File.Exists(target)) File.Replace(temp, target, null); else File.Move(temp, target);
            }
            finally { if (File.Exists(temp)) File.Delete(temp); }
        }

        internal static string QuoteArgument(string argument)
        {
            if (argument == null) throw new ArgumentNullException("argument");
            StringBuilder result = new StringBuilder("\"");
            int slashes = 0;
            foreach (char c in argument)
            {
                if (c == '\\') { slashes++; continue; }
                if (c == '"') result.Append('\\', slashes * 2 + 1); else result.Append('\\', slashes);
                result.Append(c); slashes = 0;
            }
            result.Append('\\', slashes * 2);
            return result.Append('"').ToString();
        }

        internal static string ExtractPayload(string destination)
        {
            Assembly assembly = Assembly.GetExecutingAssembly();
            string digest;
            Dictionary<string, string> manifest;
            using (Stream input = assembly.GetManifestResourceStream(ResourceName))
            {
                if (input == null) throw new InvalidDataException("软件缺少内置启动文件，请重新下载完整 EXE。");
                using (SHA256 sha = SHA256.Create()) digest = BitConverter.ToString(sha.ComputeHash(input)).Replace("-", "").ToLowerInvariant();
            }
            using (Stream input = assembly.GetManifestResourceStream(ResourceName))
            using (ZipArchive zip = new ZipArchive(input, ZipArchiveMode.Read))
            {
                ZipArchiveEntry entry = zip.GetEntry("BUNDLE_MANIFEST.json");
                if (entry == null) throw new InvalidDataException("内置启动文件缺少完整性清单。");
                using (StreamReader reader = new StreamReader(entry.Open(), Encoding.UTF8)) manifest = Json.Deserialize<Dictionary<string, string>>(reader.ReadToEnd());
            }
            if (manifest == null || !manifest.ContainsKey("app/bootstrap.ps1") || !manifest.ContainsKey("app/launcher.py")) throw new InvalidDataException("启动文件完整性清单无效。");
            string marker = Path.Combine(destination, ".payload-sha256");
            if (File.Exists(marker) && File.ReadAllText(marker).Trim() == digest && VerifyPayload(destination, manifest)) return destination;
            string parent = Path.GetDirectoryName(destination);
            Directory.CreateDirectory(parent);
            string staging = destination + ".staging-" + Guid.NewGuid().ToString("N");
            Directory.CreateDirectory(staging);
            try
            {
                using (Stream input = assembly.GetManifestResourceStream(ResourceName))
                using (ZipArchive zip = new ZipArchive(input, ZipArchiveMode.Read))
                {
                    string prefix = Path.GetFullPath(staging).TrimEnd('\\') + "\\";
                    foreach (ZipArchiveEntry entry in zip.Entries)
                    {
                        if (entry.FullName.IndexOf(':') >= 0 || Path.IsPathRooted(entry.FullName)) throw new InvalidDataException("内置文件路径无效。");
                        string path = Path.GetFullPath(Path.Combine(staging, entry.FullName.Replace('/', '\\')));
                        if (!path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("内置文件超出安装目录。");
                        if (entry.FullName.EndsWith("/", StringComparison.Ordinal) || entry.FullName.EndsWith("\\", StringComparison.Ordinal)) Directory.CreateDirectory(path);
                        else
                        {
                            Directory.CreateDirectory(Path.GetDirectoryName(path));
                            using (Stream source = entry.Open())
                            using (FileStream target = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None)) source.CopyTo(target);
                        }
                    }
                }
                if (!File.Exists(Path.Combine(staging, "app", "bootstrap.ps1")) || !File.Exists(Path.Combine(staging, "app", "launcher.py"))) throw new InvalidDataException("内置启动文件不完整。");
                if (!VerifyPayload(staging, manifest)) throw new InvalidDataException("内置启动文件校验失败，请重新下载软件。");
                File.WriteAllText(Path.Combine(staging, ".payload-sha256"), digest, Encoding.ASCII);
                // Preserve an earlier payload in case a locally modified copy needs recovery.
                if (Directory.Exists(destination)) Directory.Move(destination, destination + ".previous-" + DateTime.UtcNow.ToString("yyyyMMddHHmmss") + "-" + Guid.NewGuid().ToString("N").Substring(0, 6));
                Directory.Move(staging, destination);
            }
            catch
            {
                if (Directory.Exists(staging)) Directory.Delete(staging, true);
                throw;
            }
            return destination;
        }

        internal static bool VerifyPayload(string directory, Dictionary<string, string> manifest)
        {
            string prefix = Path.GetFullPath(directory).TrimEnd('\\') + "\\";
            foreach (KeyValuePair<string, string> item in manifest)
            {
                if (item.Key.IndexOf(':') >= 0 || Path.IsPathRooted(item.Key) || item.Value == null || item.Value.Length != 64) return false;
                string file = Path.GetFullPath(Path.Combine(directory, item.Key.Replace('/', '\\')));
                if (!file.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) || !File.Exists(file)) return false;
                string digest;
                using (FileStream input = File.OpenRead(file))
                using (SHA256 sha = SHA256.Create()) digest = BitConverter.ToString(sha.ComputeHash(input)).Replace("-", "").ToLowerInvariant();
                if (!String.Equals(digest, item.Value, StringComparison.OrdinalIgnoreCase)) return false;
            }
            return true;
        }
    }

    internal sealed class ProcessJob : IDisposable
    {
        private IntPtr handle;
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError = true)] private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);
        [DllImport("kernel32.dll", SetLastError = true)] private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll")] private static extern bool TerminateJobObject(IntPtr job, uint exitCode);
        [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
        [StructLayout(LayoutKind.Sequential)] private struct BasicLimit { public long PerProcessUserTimeLimit, PerJobUserTimeLimit; public uint LimitFlags; public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize; public uint ActiveProcessLimit; public UIntPtr Affinity; public uint PriorityClass, SchedulingClass; }
        [StructLayout(LayoutKind.Sequential)] private struct IoCounters { public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount, ReadTransferCount, WriteTransferCount, OtherTransferCount; }
        [StructLayout(LayoutKind.Sequential)] private struct ExtendedLimit { public BasicLimit BasicLimitInformation; public IoCounters IoInfo; public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed; }
        internal ProcessJob()
        {
            handle = CreateJobObject(IntPtr.Zero, null);
            if (handle == IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
            ExtendedLimit limits = new ExtendedLimit(); limits.BasicLimitInformation.LimitFlags = 0x2000;
            IntPtr buffer = Marshal.AllocHGlobal(Marshal.SizeOf(limits));
            try
            {
                Marshal.StructureToPtr(limits, buffer, false);
                if (!SetInformationJobObject(handle, 9, buffer, (uint)Marshal.SizeOf(limits))) throw new System.ComponentModel.Win32Exception();
            }
            catch { Dispose(); throw; }
            finally { Marshal.FreeHGlobal(buffer); }
        }
        internal void Add(Process process) { if (!AssignProcessToJobObject(handle, process.Handle)) throw new System.ComponentModel.Win32Exception(); }
        internal void Stop() { if (handle != IntPtr.Zero) TerminateJobObject(handle, 1); }
        public void Dispose() { if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; } }
    }

    internal static class Theme
    {
        internal static readonly Color Background = Color.FromArgb(23, 25, 31), Sidebar = Color.FromArgb(29, 32, 40), Card = Color.FromArgb(37, 42, 53), Border = Color.FromArgb(51, 57, 70), Text = Color.FromArgb(239, 242, 249), Muted = Color.FromArgb(153, 163, 183), Blue = Color.FromArgb(105, 148, 255);
        internal static GraphicsPath Round(Rectangle rect, int radius)
        {
            GraphicsPath path = new GraphicsPath(); int d = radius * 2;
            path.AddArc(rect.Left, rect.Top, d, d, 180, 90); path.AddArc(rect.Right - d, rect.Top, d, d, 270, 90);
            path.AddArc(rect.Right - d, rect.Bottom - d, d, d, 0, 90); path.AddArc(rect.Left, rect.Bottom - d, d, d, 90, 90); path.CloseFigure(); return path;
        }
        internal static void Icon(Graphics g, int kind, Rectangle box, Color color)
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            using (Pen p = new Pen(color, 1.7f))
            {
                p.LineJoin = LineJoin.Round;
                float x = box.X, y = box.Y, w = box.Width, h = box.Height;
                if (kind == 0) { g.DrawLines(p, new PointF[] { new PointF(x + 2, y + h * .45f), new PointF(x + w / 2, y + 2), new PointF(x + w - 2, y + h * .45f) }); g.DrawLines(p, new PointF[] { new PointF(x + 5, y + h * .42f), new PointF(x + 5, y + h - 3), new PointF(x + w - 5, y + h - 3), new PointF(x + w - 5, y + h * .42f) }); }
                else if (kind == 1) { g.DrawLines(p, new PointF[] { new PointF(x + 2, y + 7), new PointF(x + 2, y + 4), new PointF(x + 10, y + 4), new PointF(x + 13, y + 7), new PointF(x + w - 2, y + 7), new PointF(x + w - 2, y + h - 3), new PointF(x + 2, y + h - 3), new PointF(x + 2, y + 7) }); }
                else if (kind == 2) { g.DrawRectangle(p, x + 4, y + 2, w - 8, h - 4); for (int k = 0; k < 3; k++) g.DrawLine(p, x + 8, y + 8 + k * 5, x + w - 8, y + 8 + k * 5); }
                else if (kind == 3) { g.DrawEllipse(p, x + 2, y + 2, w - 4, h - 4); using (Font f = new Font("Segoe UI", 12, FontStyle.Bold)) using (Brush b = new SolidBrush(color)) { using (StringFormat sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center }) g.DrawString("?", f, b, box, sf); } }
                else if (kind == 4) { g.DrawRectangle(p, x + 3, y + 3, w - 6, h - 6); g.DrawLines(p, new PointF[] { new PointF(x + 5, y + h - 6), new PointF(x + 10, y + h * .5f), new PointF(x + 15, y + h - 9), new PointF(x + w - 5, y + 10) }); g.DrawEllipse(p, x + 7, y + 7, 3, 3); }
                else { g.DrawRectangle(p, x + 5, y + 5, w - 10, h - 10); for (int k = 0; k < 3; k++) { float t = 8 + k * 5; g.DrawLine(p, x + t, y + 1, x + t, y + 5); g.DrawLine(p, x + t, y + h - 5, x + t, y + h - 1); g.DrawLine(p, x + 1, y + t, x + 5, y + t); g.DrawLine(p, x + w - 5, y + t, x + w - 1, y + t); } }
            }
        }
    }

    internal class Surface : Panel
    {
        internal Color Fill = Theme.Card;
        internal Surface() { SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint, true); BackColor = Theme.Background; }
        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            if (Width > 4 && Height > 4) using (GraphicsPath path = Theme.Round(new Rectangle(0, 0, Width - 1, Height - 1), 12)) using (Brush b = new SolidBrush(Fill)) { e.Graphics.FillPath(b, path); }
            base.OnPaint(e);
        }
    }

    internal sealed class MossButton : Button
    {
        internal bool Primary, Selected; internal int Symbol = -1; private bool hover;
        internal MossButton() { SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true); FlatStyle = FlatStyle.Flat; FlatAppearance.BorderSize = 0; Cursor = Cursors.Hand; }
        protected override void OnMouseEnter(EventArgs e) { hover = true; Invalidate(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { hover = false; Invalidate(); base.OnMouseLeave(e); }
        protected override void OnEnabledChanged(EventArgs e) { Invalidate(); base.OnEnabledChanged(e); }
        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.Clear(Parent == null ? Theme.Background : Parent.BackColor); e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Color fill = !Enabled ? Color.FromArgb(44, 49, 61) : Primary ? (hover ? Color.FromArgb(155, 215, 255) : Color.FromArgb(128, 202, 255)) : Selected ? Color.FromArgb(47, 59, 85) : hover ? Color.FromArgb(47, 53, 67) : BackColor;
            Color text = !Enabled ? Color.FromArgb(117, 127, 147) : Primary ? Color.FromArgb(13, 27, 57) : Selected ? Color.FromArgb(158, 188, 255) : ForeColor;
            using (GraphicsPath path = Theme.Round(new Rectangle(0, 0, Width - 1, Height - 1), 9)) using (Brush b = new SolidBrush(fill)) { e.Graphics.FillPath(b, path); if (Focused && ShowFocusCues) using (Pen p = new Pen(Theme.Blue, 2)) e.Graphics.DrawPath(p, path); }
            if (Selected) using (Pen p = new Pen(Theme.Blue, 3)) e.Graphics.DrawLine(p, 2, 13, 2, Height - 13);
            Rectangle textRect = new Rectangle(Symbol >= 0 ? 46 : 8, 0, Width - (Symbol >= 0 ? 50 : 16), Height);
            if (Symbol >= 0) Theme.Icon(e.Graphics, Symbol, new Rectangle(15, (Height - 23) / 2, 23, 23), text);
            TextRenderer.DrawText(e.Graphics, Text, Font, textRect, text, (Symbol >= 0 ? TextFormatFlags.Left : TextFormatFlags.HorizontalCenter) | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis);
        }
    }

    internal sealed class FolderCard : Control
    {
        internal string Heading, Detail, Space; internal int Symbol; private bool hover;
        internal FolderCard() { SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw | ControlStyles.Selectable, true); Cursor = Cursors.Hand; TabStop = true; BackColor = Theme.Background; }
        protected override void OnMouseEnter(EventArgs e) { hover = true; Invalidate(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { hover = false; Invalidate(); base.OnMouseLeave(e); }
        protected override void OnKeyDown(KeyEventArgs e) { if (e.KeyCode == Keys.Enter || e.KeyCode == Keys.Space) { OnClick(EventArgs.Empty); e.Handled = true; } base.OnKeyDown(e); }
        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.Clear(BackColor); e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            using (GraphicsPath path = Theme.Round(new Rectangle(0, 0, Width - 1, Height - 1), 11)) using (Brush b = new SolidBrush(hover ? Color.FromArgb(43, 50, 66) : Theme.Card)) { e.Graphics.FillPath(b, path); if (Focused) using (Pen p = new Pen(Theme.Blue)) e.Graphics.DrawPath(p, path); }
            using (Brush b = new SolidBrush(Color.FromArgb(46, 59, 88))) e.Graphics.FillEllipse(b, 15, (Height - 38) / 2, 38, 38);
            Theme.Icon(e.Graphics, Symbol, new Rectangle(22, (Height - 24) / 2, 24, 24), Theme.Blue);
            using (Font title = new Font(Font.FontFamily, 10, FontStyle.Bold)) TextRenderer.DrawText(e.Graphics, Heading, title, new Rectangle(66, 10, Width - 95, 23), Theme.Text, TextFormatFlags.Left | TextFormatFlags.EndEllipsis);
            using (Font small = new Font(Font.FontFamily, 8.25f)) { TextRenderer.DrawText(e.Graphics, Detail ?? "", small, new Rectangle(66, 35, Width - 92, 19), Theme.Muted, TextFormatFlags.Left | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPrefix); TextRenderer.DrawText(e.Graphics, Space ?? "", small, new Rectangle(66, 55, Width - 92, 19), Color.FromArgb(135, 171, 220), TextFormatFlags.Left | TextFormatFlags.EndEllipsis); }
            using (Font arrow = new Font("Segoe UI", 20)) TextRenderer.DrawText(e.Graphics, "›", arrow, new Rectangle(Width - 29, (Height - 30) / 2, 24, 30), Theme.Muted);
        }
    }

    internal sealed class Hero : Control
    {
        internal Hero() { SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true); BackColor = Theme.Background; }
        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics; g.Clear(BackColor); g.SmoothingMode = SmoothingMode.AntiAlias;
            using (GraphicsPath path = Theme.Round(new Rectangle(0, 0, Width - 1, Height - 1), 14))
            {
                g.SetClip(path); using (LinearGradientBrush b = new LinearGradientBrush(ClientRectangle, Color.FromArgb(33, 48, 83), Color.FromArgb(48, 37, 82), 15f)) g.FillPath(b, path);
                float artX = Width * .77f, artY = Height * .57f;
                using (Pen line = new Pen(Color.FromArgb(38, 166, 191, 255), 1)) for (int i = 0; i < 8; i++) g.DrawEllipse(line, artX - 75 - i * 16, artY - 75 - i * 16, 150 + i * 32, 150 + i * 32);
                using (LinearGradientBrush b = new LinearGradientBrush(new Rectangle((int)artX - 72, (int)artY - 72, 144, 144), Color.FromArgb(172, 188, 255), Color.FromArgb(84, 72, 190), 60f)) g.FillEllipse(b, artX - 72, artY - 72, 144, 144);
                using (LinearGradientBrush b = new LinearGradientBrush(new Rectangle(0, 85, Width, Height), Color.FromArgb(34, 72, 125), Color.FromArgb(52, 29, 85), 20f)) g.FillClosedCurve(b, new PointF[] { new PointF(Width * .46f, Height + 20), new PointF(Width * .68f, Height * .64f), new PointF(Width * .83f, Height * .88f), new PointF(Width + 25, Height * .54f), new PointF(Width + 30, Height + 30) });
                using (Pen p = new Pen(Color.FromArgb(100, 182, 208, 255), 1.5f)) g.DrawBezier(p, Width * .51f, Height, Width * .68f, 72, Width * .82f, 215, Width + 20, 80);
                using (Brush b = new SolidBrush(Color.FromArgb(207, 218, 255))) { g.FillEllipse(b, artX + 101, 32, 4, 4); g.FillEllipse(b, artX - 111, 40, 3, 3); g.FillEllipse(b, artX + 85, 128, 3, 3); }
                g.ResetClip();
            }
            using (Font mini = new Font("Segoe UI", 8.5f, FontStyle.Bold)) TextRenderer.DrawText(g, "CREATE SOMETHING NEW", mini, new Point(27, 26), Color.FromArgb(154, 185, 255));
            using (Font title = new Font("Microsoft YaHei UI", 23, FontStyle.Bold)) TextRenderer.DrawText(g, "让想象，成为画面。", title, new Rectangle(24, 52, (int)(Width * .70f), 48), Color.White, TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            using (Font sub = new Font("Microsoft YaHei UI", 9.5f)) TextRenderer.DrawText(g, "MOSS-Image 本地 AI 画室\r\n写下灵感，其余交给你的电脑。", sub, new Rectangle(27, 108, (int)(Width * .58f), 52), Color.FromArgb(190, 203, 228), TextFormatFlags.Left | TextFormatFlags.WordBreak);
        }
    }

    internal sealed class ActivityBar : Control
    {
        private readonly System.Windows.Forms.Timer animation = new System.Windows.Forms.Timer();
        private ProgressBarStyle style; private int value, frame;
        internal ProgressBarStyle Style { get { return style; } set { style = value; animation.Enabled = style == ProgressBarStyle.Marquee; Invalidate(); } }
        internal int Value { get { return value; } set { this.value = Math.Max(0, Math.Min(100, value)); Invalidate(); } }
        internal ActivityBar() { SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true); animation.Interval = 35; animation.Tick += delegate { frame = (frame + 5) % Math.Max(1, Width + Width / 3); Invalidate(); }; }
        protected override void OnPaint(PaintEventArgs e)
        {
            e.Graphics.Clear(Theme.Border);
            using (Brush b = new SolidBrush(Theme.Blue)) { if (style == ProgressBarStyle.Marquee) e.Graphics.FillRectangle(b, frame - Width / 3, 0, Math.Max(20, Width / 3), Height); else if (value > 0) e.Graphics.FillRectangle(b, 0, 0, Width * value / 100, Height); }
        }
        protected override void Dispose(bool disposing) { if (disposing) animation.Dispose(); base.Dispose(disposing); }
    }

    internal sealed class MainForm : Form
    {
        private readonly TextBox[] paths = new TextBox[3];
        private readonly Label[] freeLabels = new Label[3];
        private readonly Button[] browseButtons = new Button[3];
        private readonly ComboBox sourceBox = new ComboBox();
        private readonly Button startButton = new MossButton(), stopButton = new MossButton(), studioButton = new MossButton(), outputButton = new MossButton(), saveButton = new MossButton();
        private readonly Panel pageHost = new Panel();
        private readonly Panel[] pages = new Panel[4];
        private readonly MossButton[] navButtons = new MossButton[4];
        private readonly FolderCard[] folderCards = new FolderCard[3];
        private readonly Label pageTitle = new Label(), pageSubtitle = new Label(), statusTitle = new Label(), storageNotice = new Label();
        private int selectedPage;
        private bool settingsLocked;
        private string backendPhase;
        private readonly Label statusLabel = new Label();
        private readonly TextBox logBox = new TextBox();
        private readonly ActivityBar progress = new ActivityBar();
        private readonly System.Windows.Forms.Timer pollTimer = new System.Windows.Forms.Timer();
        private readonly object logGate = new object();
        private Process bootstrap;
        private ProcessJob job;
        private Settings activeSettings;
        private string controlUrl, controlToken, studioUrl, logPath;
        private string lastBackendLog;
        private bool preparing, polling, installing, stopping, closing, closed, autoOpened, postedInstall;
        private volatile bool stopRequested;
        private int failedPolls;
        private int generation;
        private DateTime startedAt;

        internal MainForm()
        {
            Text = "MOSS-Image · 本地 AI 画室";
            Font = new Font("Microsoft YaHei UI", 9.25f);
            AutoScaleDimensions = new SizeF(96f, 96f); AutoScaleMode = AutoScaleMode.Dpi;
            Rectangle work = Screen.PrimaryScreen.WorkingArea;
            ClientSize = new Size(Math.Min(1100, work.Width - 40), Math.Min(760, work.Height - 64));
            MinimumSize = new Size(Math.Min(980, work.Width), Math.Min(650, work.Height));
            StartPosition = FormStartPosition.CenterScreen; BackColor = Theme.Background; ForeColor = Theme.Text;
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath) ?? SystemIcons.Application;
            SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint, true);

            Panel side = new Panel { Dock = DockStyle.Left, Width = 178, BackColor = Theme.Sidebar, Padding = new Padding(15, 0, 15, 0) };
            Panel brand = new Panel { Dock = DockStyle.Top, Height = 112 };
            brand.Paint += delegate(object sender, PaintEventArgs e) {
                e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
                using (Pen pen = new Pen(Theme.Blue, 3.3f)) e.Graphics.DrawLines(pen, new PointF[] { new PointF(9, 59), new PointF(9, 33), new PointF(23, 49), new PointF(37, 33), new PointF(37, 59) });
                using (Font title = new Font("Segoe UI", 14.5f, FontStyle.Bold)) TextRenderer.DrawText(e.Graphics, "MOSS", title, new Point(48, 28), Theme.Text);
                using (Font caption = new Font("Segoe UI", 8.25f)) TextRenderer.DrawText(e.Graphics, "I M A G E", caption, new Point(51, 54), Theme.Muted);
            };
            FlowLayoutPanel navigation = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 240, FlowDirection = FlowDirection.TopDown, WrapContents = false };
            string[] nav = new string[] { "首页", "存储设置", "运行日志", "使用帮助" };
            for (int i = 0; i < 4; i++)
            {
                int page = i;
                navButtons[i] = new MossButton { Text = nav[i], Symbol = i, Size = new Size(148, 46), Margin = new Padding(0, 0, 0, 8), BackColor = Theme.Sidebar, ForeColor = Theme.Muted, Font = new Font(Font.FontFamily, 10) };
                navButtons[i].Click += delegate { ShowPage(page); }; navigation.Controls.Add(navButtons[i]);
            }
            Panel sideFoot = new Panel { Dock = DockStyle.Bottom, Height = 93 };
            sideFoot.Controls.Add(MakeLabel("本地运行 · 私人创作", 0, 10, 150, 24, Theme.Muted, 8.5f, false));
            sideFoot.Controls.Add(MakeLabel("v" + AppFiles.Version + "  /  正式版", 0, 39, 150, 24, Color.FromArgb(112, 125, 150), 8.5f, false));
            side.Controls.Add(navigation); side.Controls.Add(brand); side.Controls.Add(sideFoot);

            Panel workspace = new Panel { Dock = DockStyle.Fill, BackColor = Theme.Background };
            Panel header = new Panel { Dock = DockStyle.Top, Height = 83, Padding = new Padding(24, 0, 24, 0) };
            pageTitle.Location = new Point(24, 19); pageTitle.Size = new Size(550, 29); pageTitle.Font = new Font(Font.FontFamily, 15, FontStyle.Bold); pageTitle.ForeColor = Theme.Text;
            pageSubtitle.Location = new Point(25, 51); pageSubtitle.Size = new Size(700, 22); pageSubtitle.ForeColor = Theme.Muted; pageSubtitle.Font = new Font(Font.FontFamily, 8.5f);
            Label badge = MakeLabel("DESKTOP  /  PREVIEW", 0, 25, 155, 22, Color.FromArgb(126, 145, 185), 8f, false); badge.TextAlign = ContentAlignment.MiddleRight;
            header.Resize += delegate { badge.Left = header.ClientSize.Width - badge.Width - 25; };
            header.Controls.Add(pageTitle); header.Controls.Add(pageSubtitle); header.Controls.Add(badge);
            pageHost.Dock = DockStyle.Fill; pageHost.Padding = new Padding(24, 0, 24, 14); pageHost.BackColor = Theme.Background;
            for (int i = 0; i < pages.Length; i++) { pages[i] = new Panel { Dock = DockStyle.Fill, BackColor = Theme.Background, Visible = false }; pageHost.Controls.Add(pages[i]); }
            BuildHome(pages[0]); BuildStorage(pages[1]); BuildLogs(pages[2]); BuildHelp(pages[3]);
            Panel footer = BuildFooter();
            workspace.Controls.Add(pageHost); workspace.Controls.Add(header); workspace.Controls.Add(footer);
            Controls.Add(workspace); Controls.Add(side);

            startButton.Click += async delegate { await StartAsync(); };
            stopButton.Click += async delegate { await StopAsync(false); };
            studioButton.Click += delegate { OpenStudio(); };
            outputButton.Click += async delegate { await OpenOutputAsync(); };
            saveButton.Click += delegate { SaveFormSettings(); };
            statusLabel.Click += delegate { ShowPage(2); };
            statusLabel.TextChanged += delegate { UpdateStatusHeading(); };
            string notice; Settings settings = AppFiles.ReadSettings(out notice);
            paths[0].Text = settings.model_dir; paths[1].Text = settings.data_dir; paths[2].Text = settings.output_dir;
            sourceBox.SelectedIndex = settings.source == "huggingface" ? 1 : 0;
            AppendLog("欢迎使用 MOSS-Image。首次启动会检查设备，再下载专用环境和模型。\r\n三个存放目录可以分别选择磁盘；关闭软件会停止后台服务。");
            if (notice != null) AppendLog(notice);
            statusLabel.Text = "先确认存储位置，点击右侧按钮开始。";
            ShowPage(0); RefreshSummaries();
            pollTimer.Interval = 1300; pollTimer.Tick += async delegate { await PollAsync(); }; FormClosing += OnFormClosing;
        }

        private Label MakeLabel(string text, int x, int y, int width, int height, Color color, float size, bool bold)
        {
            return new Label { Text = text, Location = new Point(x, y), Size = new Size(width, height), ForeColor = color, BackColor = Color.Transparent, Font = new Font(Font.FontFamily, size, bold ? FontStyle.Bold : FontStyle.Regular) };
        }
        private void BuildHome(Panel page)
        {
            page.AutoScroll = true;
            TableLayoutPanel layout = new TableLayoutPanel { Dock = DockStyle.Top, Height = 480, ColumnCount = 1, RowCount = 3, Margin = Padding.Empty };
            page.Resize += delegate { layout.Height = Math.Max(480, page.ClientSize.Height); };
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 39)); layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 17)); layout.RowStyles.Add(new RowStyle(SizeType.Percent, 61));
            Hero hero = new Hero { Dock = DockStyle.Fill, Margin = Padding.Empty }; layout.Controls.Add(hero, 0, 0);
            TableLayoutPanel lower = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, RowCount = 1, Margin = Padding.Empty };
            lower.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 66)); lower.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 16)); lower.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 34));
            TableLayoutPanel cards = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4, Margin = Padding.Empty };
            cards.RowStyles.Add(new RowStyle(SizeType.Absolute, 28)); for (int i = 0; i < 3; i++) cards.RowStyles.Add(new RowStyle(SizeType.Percent, 33.333f));
            cards.Controls.Add(new Label { Text = "我的工作空间", Dock = DockStyle.Fill, ForeColor = Theme.Muted, Font = new Font(Font.FontFamily, 9, FontStyle.Bold), Margin = Padding.Empty }, 0, 0);
            string[] names = new string[] { "模型库", "环境与缓存", "我的作品" };
            for (int i = 0; i < 3; i++)
            {
                int index = i;
                folderCards[i] = new FolderCard { Heading = names[i], Symbol = i == 0 ? 1 : i == 1 ? 5 : 4, Dock = DockStyle.Fill, Margin = new Padding(0, 0, 0, i == 2 ? 0 : 8), Font = Font, AccessibleName = names[i], AccessibleRole = AccessibleRole.PushButton };
                folderCards[i].Click += async delegate { if (index == 2) await OpenOutputAsync(); else { ShowPage(1); paths[index].Focus(); paths[index].SelectAll(); } };
                cards.Controls.Add(folderCards[i], 0, i + 1);
            }
            Surface note = new Surface { Dock = DockStyle.Fill, Margin = new Padding(0, 28, 0, 0), Padding = new Padding(19, 18, 19, 17) };
            TableLayoutPanel noteContent = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4, BackColor = Theme.Card, Margin = Padding.Empty };
            noteContent.RowStyles.Add(new RowStyle(SizeType.Absolute, 29)); noteContent.RowStyles.Add(new RowStyle(SizeType.Absolute, 33)); noteContent.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); noteContent.RowStyles.Add(new RowStyle(SizeType.Absolute, 34));
            noteContent.Controls.Add(new Label { Text = "从第一张图片开始", Dock = DockStyle.Fill, ForeColor = Theme.Text, Font = new Font(Font.FontFamily, 10.5f, FontStyle.Bold), Margin = Padding.Empty }, 0, 0);
            noteContent.Controls.Add(new Label { Text = "首次下载 · 之后本地使用", Dock = DockStyle.Fill, ForeColor = Theme.Blue, Font = new Font(Font.FontFamily, 8.25f), Margin = Padding.Empty }, 0, 1);
            noteContent.Controls.Add(new Label { Text = "01  选择空间充足的磁盘\r\n\r\n02  一键准备环境与模型\r\n\r\n03  在浏览器画室中创作", Dock = DockStyle.Fill, ForeColor = Theme.Muted, Font = new Font(Font.FontFamily, 9f), Margin = Padding.Empty }, 0, 2);
            MossButton help = new MossButton { Text = "查看使用帮助  →", Dock = DockStyle.Fill, BackColor = Color.FromArgb(45, 54, 73), ForeColor = Color.FromArgb(162, 190, 255), Margin = Padding.Empty }; help.Click += delegate { ShowPage(3); }; noteContent.Controls.Add(help, 0, 3); note.Controls.Add(noteContent);
            lower.Controls.Add(cards, 0, 0); lower.Controls.Add(note, 2, 0); layout.Controls.Add(lower, 0, 2); page.Controls.Add(layout);
        }
        private void BuildStorage(Panel page)
        {
            page.AutoScroll = true;
            Panel content = new Panel { Dock = DockStyle.Top, Size = new Size(810, 522), BackColor = Theme.Background };
            string[] titles = new string[] { "模型存放目录", "环境与缓存目录", "作品保存目录" };
            string[] descriptions = new string[] { "模型约 27.22 GB，建议选择空间充足的磁盘", "专用 Python、依赖、下载缓存和临时文件", "生成的图片与参数文件，在这里集中保存" };
            for (int i = 0; i < 3; i++)
            {
                int index = i;
                Surface card = new Surface { Location = new Point(0, i * 114), Size = new Size(810, 103), Anchor = AnchorStyles.Left | AnchorStyles.Top | AnchorStyles.Right };
                Label title = MakeLabel(titles[i], 17, 13, 170, 23, Theme.Text, 10, true);
                Label description = MakeLabel(descriptions[i], 190, 16, 560, 22, Theme.Muted, 8.25f, false); description.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
                paths[i] = new TextBox { Location = new Point(20, 43), Size = new Size(686, 25), Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right, BorderStyle = BorderStyle.FixedSingle, BackColor = Color.FromArgb(24, 28, 37), ForeColor = Theme.Text, Font = new Font(Font.FontFamily, 9), AccessibleName = titles[i] };
                freeLabels[i] = MakeLabel("", 18, 75, 690, 20, Color.FromArgb(136, 167, 212), 8.25f, false); freeLabels[i].Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
                browseButtons[i] = NewButton("选择…", Color.FromArgb(49, 58, 77), Theme.Text); browseButtons[i].Location = new Point(716, 42); browseButtons[i].Size = new Size(77, 29); browseButtons[i].Anchor = AnchorStyles.Top | AnchorStyles.Right; browseButtons[i].Click += delegate { Browse(index); };
                paths[i].TextChanged += delegate { RefreshFree(index); };
                card.Controls.Add(title); card.Controls.Add(description); card.Controls.Add(paths[i]); card.Controls.Add(freeLabels[i]); card.Controls.Add(browseButtons[i]); content.Controls.Add(card);
            }
            Label downloadLabel = MakeLabel("模型下载源", 1, 351, 120, 27, Theme.Text, 9.25f, true); content.Controls.Add(downloadLabel);
            sourceBox.DropDownStyle = ComboBoxStyle.DropDownList; sourceBox.FlatStyle = FlatStyle.Flat; sourceBox.BackColor = Theme.Card; sourceBox.ForeColor = Theme.Text; sourceBox.Location = new Point(126, 348); sourceBox.Size = new Size(315, 30);
            sourceBox.Items.AddRange(new object[] { "官方魔搭 ModelScope（国内建议）", "官方 Hugging Face" }); sourceBox.SelectedIndex = 0; content.Controls.Add(sourceBox);
            sourceBox.DrawMode = DrawMode.OwnerDrawFixed; sourceBox.ItemHeight = 23;
            sourceBox.DrawItem += delegate(object sender, DrawItemEventArgs e) {
                Color fill = (e.State & DrawItemState.Selected) != 0 ? Color.FromArgb(47, 59, 85) : Theme.Card;
                using (Brush b = new SolidBrush(fill)) e.Graphics.FillRectangle(b, e.Bounds);
                if (e.Index >= 0) TextRenderer.DrawText(e.Graphics, Convert.ToString(sourceBox.Items[e.Index]), sourceBox.Font, new Rectangle(e.Bounds.X + 8, e.Bounds.Y, e.Bounds.Width - 12, e.Bounds.Height), sourceBox.Enabled ? Theme.Text : Theme.Muted, TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
                e.DrawFocusRectangle();
            };
            storageNotice.Text = "三个目录可分放不同磁盘，建议总计预留 60 GB。\r\n更换位置不会自动搬移或删除旧文件；选择已有完整模型的目录可复用文件。";
            storageNotice.Location = new Point(0, 396); storageNotice.Size = new Size(800, 57); storageNotice.ForeColor = Theme.Muted; storageNotice.Font = new Font(Font.FontFamily, 9); storageNotice.Anchor = AnchorStyles.Left | AnchorStyles.Top | AnchorStyles.Right; content.Controls.Add(storageNotice);
            StyleButton(saveButton, "保存设置", Theme.Blue, Color.FromArgb(13, 27, 57), 135); ((MossButton)saveButton).Primary = true; saveButton.Location = new Point(0, 466); saveButton.Height = 40; content.Controls.Add(saveButton);
            content.Controls.Add(MakeLabel("只保存位置，准备好后再启动。", 153, 477, 480, 24, Theme.Muted, 8.5f, false));
            page.Controls.Add(content);
        }
        private void BuildLogs(Panel page)
        {
            Surface card = new Surface { Dock = DockStyle.Fill, Padding = new Padding(20) };
            Label intro = new Label { Text = "实时运行记录", Dock = DockStyle.Top, Height = 36, ForeColor = Theme.Text, BackColor = Theme.Card, Font = new Font(Font.FontFamily, 10, FontStyle.Bold) };
            Label hint = new Label { Text = "遇到问题时，可选中并复制日志。文件保存在环境与缓存目录的 logs 文件夹。", Dock = DockStyle.Bottom, Height = 30, ForeColor = Theme.Muted, BackColor = Theme.Card, Font = new Font(Font.FontFamily, 8.25f), TextAlign = ContentAlignment.BottomLeft };
            logBox.Dock = DockStyle.Fill; logBox.ReadOnly = true; logBox.Multiline = true; logBox.ScrollBars = ScrollBars.Vertical; logBox.BorderStyle = BorderStyle.None; logBox.BackColor = Theme.Card; logBox.ForeColor = Color.FromArgb(185, 199, 222); logBox.Font = new Font("Microsoft YaHei UI", 9f);
            card.Controls.Add(logBox); card.Controls.Add(intro); card.Controls.Add(hint); page.Controls.Add(card);
        }
        private void BuildHelp(Panel page)
        {
            page.AutoScroll = true;
            Panel content = new Panel { Dock = DockStyle.Top, Size = new Size(810, 525), BackColor = Theme.Background };
            string[] titles = new string[] { "01   先选一个宽敞的地方", "02   点一次，准备好整个画室", "03   写下提示词，生成第一张图" };
            string[] body = new string[] { "在「存储设置」里分别选择模型、环境缓存、作品目录。\r\n首次安装建议总计预留 60 GB；目录可放在不同磁盘。", "点击「一键启动」，软件会先检查设备，再下载环境和模型。\r\n可以停止后继续；已完成的下载会保留。", "准备完成后，默认浏览器会自动打开本地画室。\r\n之后使用「打开画室」再次进入，用「查看作品」找到图片。" };
            for (int i = 0; i < 3; i++)
            {
                Surface card = new Surface { Location = new Point(0, i * 117), Size = new Size(810, 106), Anchor = AnchorStyles.Left | AnchorStyles.Top | AnchorStyles.Right };
                card.Controls.Add(MakeLabel(titles[i], 20, 15, 730, 27, i == 0 ? Theme.Blue : Theme.Text, 10.5f, true));
                Label description = MakeLabel(body[i], 20, 48, 740, 48, Theme.Muted, 9, false); description.Anchor = AnchorStyles.Left | AnchorStyles.Top | AnchorStyles.Right; card.Controls.Add(description); content.Controls.Add(card);
            }
            Label scope = MakeLabel("关于 MOSS-Image 桌面版", 0, 367, 700, 30, Theme.Text, 10.5f, true); content.Controls.Add(scope);
            Label details = MakeLabel("面向 Windows 10/11 + NVIDIA 显卡。运行效果取决于设备配置与模型设置，8 GB 显存不保证可用。\r\n环境与缓存目录保存运行环境、依赖、临时文件与日志；模型和作品分别保存在各自目录。\r\n小型启动文件与设置保存在用户 AppData。关闭软件会停止安装和画室服务。", 0, 406, 810, 100, Theme.Muted, 9, false); details.Anchor = AnchorStyles.Left | AnchorStyles.Top | AnchorStyles.Right; content.Controls.Add(details); page.Controls.Add(content);
        }
        private Panel BuildFooter()
        {
            Panel footer = new Panel { Dock = DockStyle.Bottom, Height = 120, Padding = new Padding(24, 14, 24, 13), BackColor = Theme.Sidebar };
            footer.Paint += delegate(object sender, PaintEventArgs e) { using (Pen line = new Pen(Theme.Border)) e.Graphics.DrawLine(line, 0, 0, footer.Width, 0); };
            Panel actions = new Panel { Dock = DockStyle.Right, Width = 326, BackColor = Theme.Sidebar };
            StyleButton(startButton, "一键启动", Theme.Blue, Color.FromArgb(13, 27, 57), 326); ((MossButton)startButton).Primary = true; startButton.Font = new Font(Font.FontFamily, 13, FontStyle.Bold); startButton.Location = new Point(0, 0); startButton.Size = new Size(326, 46);
            StyleButton(stopButton, "停止", Theme.Card, Theme.Muted, 74); stopButton.Location = new Point(0, 57); stopButton.Size = new Size(74, 30);
            StyleButton(studioButton, "打开画室", Theme.Card, Theme.Text, 113); studioButton.Location = new Point(84, 57); studioButton.Size = new Size(113, 30);
            StyleButton(outputButton, "查看作品", Theme.Card, Theme.Text, 119); outputButton.Location = new Point(207, 57); outputButton.Size = new Size(119, 30);
            stopButton.Enabled = false; studioButton.Enabled = false; actions.Controls.AddRange(new Control[] { startButton, stopButton, studioButton, outputButton });
            Panel status = new Panel { Dock = DockStyle.Fill, Padding = new Padding(0, 0, 22, 0), BackColor = Theme.Sidebar };
            statusTitle.Dock = DockStyle.Top; statusTitle.Height = 25; statusTitle.Text = "●  准备就绪"; statusTitle.ForeColor = Color.FromArgb(143, 204, 184); statusTitle.Font = new Font(Font.FontFamily, 9.5f, FontStyle.Bold);
            statusLabel.Dock = DockStyle.Fill; statusLabel.ForeColor = Theme.Muted; statusLabel.Font = new Font(Font.FontFamily, 8.5f); statusLabel.Cursor = Cursors.Hand; statusLabel.AutoEllipsis = true; statusLabel.AccessibleName = "状态；点击查看运行日志";
            progress.Dock = DockStyle.Bottom; progress.Height = 4; progress.Style = ProgressBarStyle.Continuous;
            status.Controls.Add(statusLabel); status.Controls.Add(statusTitle); status.Controls.Add(progress); footer.Controls.Add(status); footer.Controls.Add(actions); return footer;
        }
        private void ShowPage(int index)
        {
            selectedPage = index;
            string[] titles = new string[] { "你的创作，从这里开始", "存储设置", "运行日志", "使用帮助" };
            string[] subtitles = new string[] { "一个入口，准备好你的本地 AI 画室。", "大文件存在哪里，由你决定。", "查看下载、安装与画室的实时状态。", "从安装到创作，只需要三步。" };
            pageTitle.Text = titles[index]; pageSubtitle.Text = subtitles[index];
            for (int i = 0; i < pages.Length; i++) { pages[i].Visible = i == index; navButtons[i].Selected = i == index; navButtons[i].Invalidate(); }
            pages[index].BringToFront(); if (index == 0) RefreshSummaries();
        }
        private Button NewButton(string caption, Color back, Color fore) { Button button = new MossButton(); StyleButton(button, caption, back, fore, 85); return button; }
        private void StyleButton(Button button, string caption, Color back, Color fore, int width)
        {
            button.Text = caption; button.BackColor = back; button.ForeColor = fore; button.FlatStyle = FlatStyle.Flat; button.FlatAppearance.BorderSize = 0;
            button.Size = new Size(width, 36); button.Margin = Padding.Empty; button.Cursor = Cursors.Hand; button.UseVisualStyleBackColor = false;
        }
        private void Browse(int index)
        {
            using (FolderBrowserDialog dialog = new FolderBrowserDialog { Description = "选择独立的存放文件夹（可新建文件夹）", ShowNewFolderButton = true })
            { if (Directory.Exists(paths[index].Text)) dialog.SelectedPath = paths[index].Text; if (dialog.ShowDialog(this) == DialogResult.OK) paths[index].Text = dialog.SelectedPath; }
        }
        private void RefreshFree(int index)
        {
            if (paths[index] == null || freeLabels[index] == null) return;
            try { string root = Path.GetPathRoot(paths[index].Text); DriveInfo drive = new DriveInfo(root); freeLabels[index].Text = drive.IsReady ? root + "   可用 " + (drive.AvailableFreeSpace / 1073741824.0).ToString("N1") + " GiB" : "磁盘暂不可用"; }
            catch { freeLabels[index].Text = "选择本机磁盘上的文件夹"; }
            if (folderCards[index] != null) { folderCards[index].Detail = paths[index].Text; folderCards[index].Space = freeLabels[index].Text; folderCards[index].Invalidate(); }
        }
        private void RefreshSummaries() { for (int i = 0; i < paths.Length; i++) RefreshFree(i); }
        private Settings FormSettings() { return new Settings { schema_version = 2, model_dir = paths[0].Text, data_dir = paths[1].Text, output_dir = paths[2].Text, source = sourceBox.SelectedIndex == 1 ? "huggingface" : "modelscope" }; }
        private void SaveFormSettings()
        {
            if (settingsLocked || preparing || bootstrap != null || stopping) return;
            try
            {
                Settings settings = FormSettings(); AppFiles.ValidateSettings(settings, true); AppFiles.SaveSettings(settings, AppFiles.ConfigPath);
                paths[0].Text = settings.model_dir; paths[1].Text = settings.data_dir; paths[2].Text = settings.output_dir;
                statusLabel.Text = "存储设置已保存。准备好后点击「一键启动」。"; AppendLog("目录设置已保存；尚未开始下载或安装。"); RefreshSummaries();
            }
            catch (Exception exc) { statusLabel.Text = "设置未保存 · " + exc.Message; AppendLog("保存设置失败：" + exc.Message); }
        }
        private void LockSettings(bool locked)
        {
            settingsLocked = locked;
            foreach (TextBox path in paths) { path.ReadOnly = locked; path.ForeColor = locked ? Theme.Muted : Theme.Text; }
            foreach (Button button in browseButtons) button.Enabled = !locked;
            sourceBox.Enabled = !locked; saveButton.Enabled = !locked; startButton.Enabled = !locked; stopButton.Enabled = locked && !stopping;
            startButton.Text = locked ? "正在准备 / 运行中" : "一键启动";
            storageNotice.Text = locked ? "正在安装或运行，目录暂时锁定。停止后即可修改设置。\r\n更换位置不会自动搬移或删除旧文件。" : "三个目录可分放不同磁盘，建议总计预留 60 GB。\r\n更换位置不会自动搬移或删除旧文件；选择已有完整模型的目录可复用文件。";
            RefreshSummaries(); UpdateStatusHeading();
        }
        private void UpdateStatusHeading()
        {
            bool error = backendPhase == "error" || backendPhase == "connection_error" || statusLabel.Text.Contains("失败") || statusLabel.Text.Contains("未能") || statusLabel.Text.Contains("无法") || statusLabel.Text.Contains("退出") || statusLabel.Text.Contains("未保存");
            bool ready = backendPhase == "ready";
            statusTitle.Text = error ? "●  需要处理 · 点击状态查看日志" : ready ? "●  画室已就绪" : settingsLocked ? "●  正在准备画室" : "●  准备就绪";
            statusTitle.ForeColor = error ? Color.FromArgb(238, 177, 130) : settingsLocked && !ready ? Theme.Blue : Color.FromArgb(143, 204, 184);
        }

        [DllImport("dwmapi.dll")] private static extern int DwmSetWindowAttribute(IntPtr window, int attribute, ref int value, int size);
        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            try { int enabled = 1; DwmSetWindowAttribute(Handle, 20, ref enabled, sizeof(int)); }
            catch (DllNotFoundException) { } catch (EntryPointNotFoundException) { }
        }

        internal void RenderSelfTests(string report, List<string> results)
        {
            string original = paths[0].Text; paths[0].Text = original + "-unsaved-test";
            for (int i = 0; i < 4; i++) { ShowPage(i); if (selectedPage != i || !pages[i].Visible || !navButtons[i].Selected) throw new Exception("Navigation did not activate the selected page."); }
            if (paths[0].Text != original + "-unsaved-test" || bootstrap != null || preparing) throw new Exception("Navigation changed unsaved settings or started installation.");
            paths[0].Text = original; results.Add("PASS: All four navigation pages switch without downloads; unsaved settings survive navigation.");
            LockSettings(true);
            for (int i = 0; i < 3; i++) if (!paths[i].ReadOnly || browseButtons[i].Enabled) throw new Exception("Path controls were not locked.");
            if (sourceBox.Enabled || saveButton.Enabled || startButton.Enabled) throw new Exception("Source or save controls were not locked.");
            ShowPage(1); LockSettings(false);
            for (int i = 0; i < 3; i++) if (paths[i].ReadOnly || !browseButtons[i].Enabled) throw new Exception("Path controls did not unlock.");
            if (!sourceBox.Enabled || !saveButton.Enabled || !startButton.Enabled) throw new Exception("Source or save controls did not unlock.");
            results.Add("PASS: Running locks all path, browse, source and save controls; stopping restores them; navigation remains active.");
            string previousMessage = statusLabel.Text;
            backendPhase = "error"; statusLabel.Text = "当前设备需要更新驱动。"; UpdateStatusHeading();
            if (!statusTitle.Text.Contains("需要处理")) throw new Exception("Backend error phase was hidden by neutral message wording.");
            backendPhase = "ready"; statusLabel.Text = "可以开始创作。"; UpdateStatusHeading();
            if (!statusTitle.Text.Contains("已就绪")) throw new Exception("Backend ready phase did not appear in the status heading.");
            backendPhase = "connection_error"; UpdateStatusHeading(); if (!statusTitle.Text.Contains("需要处理")) throw new Exception("Connection error was not visible.");
            backendPhase = null; statusLabel.Text = previousMessage; UpdateStatusHeading();
            results.Add("PASS: Explicit error, ready and connection states display accurately regardless of message wording.");
            string[] names = new string[] { "home", "storage", "logs", "help" }; string prefix = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(report)), Path.GetFileNameWithoutExtension(report));
            ClientSize = new Size(1100, 760);
            for (int i = 0; i < 4; i++) { ShowPage(i); PerformLayout(); Application.DoEvents(); using (Bitmap preview = new Bitmap(Width, Height)) { DrawToBitmap(preview, new Rectangle(Point.Empty, Size)); preview.Save(prefix + "." + names[i] + ".png", System.Drawing.Imaging.ImageFormat.Png); } }
            Size = MinimumSize; ShowPage(1); PerformLayout(); Application.DoEvents();
            for (int i = 0; i < 3; i++) if (browseButtons[i].Right > browseButtons[i].Parent.ClientSize.Width || paths[i].Right >= browseButtons[i].Left) throw new Exception("Storage browse button or input overflows at minimum window size.");
            ShowPage(0); PerformLayout(); Application.DoEvents();
            foreach (FolderCard card in folderCards) if (card.Height < 74) throw new Exception("Home folder card is too short to show its free-space line.");
            if (startButton.PointToScreen(Point.Empty).Y + startButton.Height > PointToScreen(new Point(0, ClientSize.Height)).Y) throw new Exception("Start button falls outside the minimum window.");
            using (Bitmap preview = new Bitmap(Width, Height)) { DrawToBitmap(preview, new Rectangle(Point.Empty, Size)); preview.Save(prefix + ".minimum.png", System.Drawing.Imaging.ImageFormat.Png); }
            results.Add("PASS: Home, storage, logs, help and minimum-window views render offline; primary action and all folder browse buttons remain in bounds; cards preserve readable height. No user settings written.");
        }
        private void OnUi(Action action) { if (IsDisposed || closed) return; try { BeginInvoke(action); } catch (InvalidOperationException) { } }
        private void AppendLog(string text)
        {
            if (String.IsNullOrWhiteSpace(text)) return;
            string line = "[" + DateTime.Now.ToString("HH:mm:ss") + "] " + text.TrimEnd() + Environment.NewLine;
            logBox.AppendText(line);
            if (logBox.TextLength > 110000) { logBox.Select(0, logBox.TextLength - 80000); logBox.SelectedText = String.Empty; }
            logBox.SelectionStart = logBox.TextLength; logBox.ScrollToCaret();
            if (!String.IsNullOrEmpty(logPath))
            {
                try { lock (logGate) File.AppendAllText(logPath, line, new UTF8Encoding(false)); }
                catch (Exception) { /* A full or disconnected data drive must not crash the controller. */ }
            }
        }

        private async Task StartAsync()
        {
            if (preparing || bootstrap != null || stopping) return;
            stopRequested = false; preparing = true; postedInstall = false; autoOpened = false; failedPolls = 0; lastBackendLog = null; backendPhase = null;
            generation++; int run = generation;
            LockSettings(true); progress.Style = ProgressBarStyle.Marquee;
            statusLabel.Text = "正在检查目录并准备启动文件…";
            try
            {
                Settings settings = FormSettings();
                await Task.Run(delegate { AppFiles.ValidateSettings(settings, true); AppFiles.SaveSettings(settings, AppFiles.ConfigPath); });
                if (stopRequested || run != generation) return;
                activeSettings = settings;
                Directory.CreateDirectory(Path.Combine(settings.data_dir, "logs"));
                logPath = Path.Combine(settings.data_dir, "logs", "desktop.log");
                AppendLog("已保存目录。模型：" + settings.model_dir + "；环境与缓存：" + settings.data_dir + "；作品：" + settings.output_dir);
                string payload = await Task.Run(delegate { return AppFiles.ExtractPayload(Path.Combine(AppFiles.Home, "app-" + AppFiles.Version)); });
                if (stopRequested || run != generation) return;
                job = new ProcessJob();
                Process process = new Process();
                process.StartInfo = new ProcessStartInfo {
                    FileName = Path.Combine(Environment.SystemDirectory, "WindowsPowerShell", "v1.0", "powershell.exe"),
                    Arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + AppFiles.QuoteArgument(Path.Combine(payload, "app", "bootstrap.ps1")) + " -ConfigPath " + AppFiles.QuoteArgument(AppFiles.ConfigPath) + " -Desktop",
                    WorkingDirectory = payload, UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden,
                    RedirectStandardOutput = true, RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
                };
                process.StartInfo.EnvironmentVariables["PYTHONUTF8"] = "1";
                process.StartInfo.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
                process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) OnUi(delegate { if (run == generation) HandleOutput(e.Data); }); };
                process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) OnUi(delegate { if (run == generation) AppendLog(e.Data); }); };
                bootstrap = process;
                if (!process.Start()) throw new IOException("启动程序未能运行。");
                try { job.Add(process); }
                catch { KillProcessTree(process); throw new IOException("无法为后台进程启用自动清理。请重新打开软件再试。"); }
                process.BeginOutputReadLine(); process.BeginErrorReadLine();
                startedAt = DateTime.UtcNow;
                statusLabel.Text = "正在准备专用运行环境；首次启动需要联网，可在「运行日志」查看进度。";
                pollTimer.Start();
            }
            catch (Exception exc)
            {
                backendPhase = "error";
                AppendLog("无法开始：" + exc.Message); statusLabel.Text = "未能启动 · " + exc.Message;
                CleanupProcesses(); LockSettings(false); progress.Style = ProgressBarStyle.Continuous;
            }
            finally
            {
                preparing = false;
                if (bootstrap == null && !stopping && !closing) { LockSettings(false); progress.Style = ProgressBarStyle.Continuous; }
            }
        }

        private void HandleOutput(string line)
        {
            const string prefix = "MOSS_CONTROL:";
            int control = line.IndexOf(prefix, StringComparison.Ordinal);
            if (control >= 0)
            {
                try
                {
                    Dictionary<string, object> value = AppFiles.Json.Deserialize<Dictionary<string, object>>(line.Substring(control + prefix.Length));
                    string url = StringValue(value, "url"), token = StringValue(value, "token");
                    Uri uri;
                    if (!Uri.TryCreate(url, UriKind.Absolute, out uri) || uri.Scheme != "http" || uri.Host != "127.0.0.1" || uri.AbsolutePath != "/" || !String.IsNullOrEmpty(uri.Query) || !String.IsNullOrEmpty(uri.Fragment) || !String.IsNullOrEmpty(uri.UserInfo) || String.IsNullOrEmpty(token)) throw new InvalidDataException();
                    controlUrl = url.TrimEnd('/'); controlToken = token; AppendLog("本地控制服务已就绪，正在检查设备并开始安装。");
                }
                catch { AppendLog("启动器返回了无效的本地控制地址。"); }
                return; // Never write the authentication token into the log.
            }
            AppendLog(line);
        }
        private static string StringValue(Dictionary<string, object> value, string key) { object result; return value.TryGetValue(key, out result) && result != null ? Convert.ToString(result) : null; }
        private static bool BoolValue(Dictionary<string, object> value, string key) { object result; return value.TryGetValue(key, out result) && result is bool && (bool)result; }
        private async Task<Dictionary<string, object>> ApiAsync(string path, string method, object body, int timeout)
        {
            string endpoint = controlUrl, token = controlToken;
            if (String.IsNullOrEmpty(endpoint)) throw new InvalidOperationException("本地服务尚未准备好。");
            return await Task.Run(delegate {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create(endpoint + path);
                request.Method = method; request.Proxy = null; request.AllowAutoRedirect = false; request.Timeout = timeout; request.ReadWriteTimeout = timeout; request.Headers["X-Launcher-Token"] = token;
                if (method == "POST")
                {
                    byte[] bytes = Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(body ?? new Dictionary<string, object>()));
                    request.ContentType = "application/json"; request.ContentLength = bytes.Length;
                    using (Stream output = request.GetRequestStream()) output.Write(bytes, 0, bytes.Length);
                }
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                using (StreamReader reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
                { string json = reader.ReadToEnd(); return String.IsNullOrWhiteSpace(json) ? new Dictionary<string, object>() : new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(json); }
            });
        }

        private async Task PollAsync()
        {
            if (polling || stopping || closing || bootstrap == null) return;
            polling = true; int run = generation;
            try
            {
                if (bootstrap.HasExited)
                {
                    backendPhase = "error";
                    int code = bootstrap.ExitCode; pollTimer.Stop(); CleanupProcesses(); LockSettings(false); progress.Style = ProgressBarStyle.Continuous;
                    statusLabel.Text = "启动服务已退出（代码 " + code + "）。检查日志后可重试。"; AppendLog(statusLabel.Text); return;
                }
                if (String.IsNullOrEmpty(controlUrl))
                {
                    if ((DateTime.UtcNow - startedAt).TotalSeconds > 45) statusLabel.Text = "仍在准备运行环境；首次联网下载可能需要数分钟，可以停止后继续。";
                    return;
                }
                Dictionary<string, object> status = await ApiAsync("/api/status", "GET", null, 4000);
                if (run != generation || stopping || closing) return;
                failedPolls = 0;
                backendPhase = StringValue(status, "phase");
                statusLabel.Text = StringValue(status, "message") ?? "本地服务正在运行。";
                UpdateStatusHeading();
                installing = BoolValue(status, "busy");
                string phase = backendPhase;
                progress.Style = installing ? ProgressBarStyle.Marquee : ProgressBarStyle.Continuous;
                progress.Value = phase == "ready" ? 100 : 0;
                studioUrl = StringValue(status, "app_url"); studioButton.Enabled = IsStudioUrl(studioUrl);
                object logs;
                if (status.TryGetValue("logs", out logs) && logs is IEnumerable)
                {
                    List<string> lines = new List<string>();
                    foreach (object line in (IEnumerable)logs) if (line != null) lines.Add(Convert.ToString(line));
                    int first = 0;
                    if (lastBackendLog != null) { int last = lines.LastIndexOf(lastBackendLog); if (last >= 0) first = last + 1; }
                    for (int i = first; i < lines.Count; i++) AppendLog(lines[i]);
                    if (lines.Count > 0) lastBackendLog = lines[lines.Count - 1];
                }
                if (!postedInstall)
                {
                    postedInstall = true;
                    await ApiAsync("/api/install", "POST", new Dictionary<string, object> { { "source", activeSettings.source } }, 7000);
                }
                if (phase == "ready" && studioButton.Enabled && !autoOpened) { autoOpened = true; OpenStudio(); }
                if (phase == "error") statusLabel.Text += "  停止后可更换设置并重试。";
            }
            catch (Exception exc)
            {
                if (run != generation || stopping || closing) return;
                failedPolls++;
                if (failedPolls == 1) AppendLog("连接本地服务暂未成功：" + exc.Message);
                if (failedPolls >= 3) { backendPhase = "connection_error"; statusLabel.Text = "暂时无法连接本地服务。可点击「停止」后重试。"; UpdateStatusHeading(); }
            }
            finally { polling = false; }
        }

        private bool IsStudioUrl(string url)
        {
            Uri uri;
            return Uri.TryCreate(url, UriKind.Absolute, out uri) && uri.Scheme == "http" && uri.Host == "127.0.0.1" && String.IsNullOrEmpty(uri.UserInfo);
        }
        private void OpenStudio()
        {
            if (!IsStudioUrl(studioUrl)) return;
            try { Process.Start(new ProcessStartInfo(studioUrl) { UseShellExecute = true }); }
            catch (Exception exc) { AppendLog("打开浏览器失败：" + exc.Message); }
        }
        private async Task OpenOutputAsync()
        {
            try
            {
                if (!String.IsNullOrEmpty(controlUrl) && !stopping) await ApiAsync("/api/open-output", "POST", null, 4000);
                else
                {
                    string folder = AppFiles.NormalizeDirectory(paths[2].Text); Directory.CreateDirectory(folder);
                    Process.Start(new ProcessStartInfo("explorer.exe", AppFiles.QuoteArgument(folder)) { UseShellExecute = false });
                }
            }
            catch (Exception exc) { AppendLog("无法打开作品目录：" + exc.Message); }
        }

        private async Task StopAsync(bool exit)
        {
            if (stopping) return;
            stopping = true; stopRequested = true; generation++; pollTimer.Stop(); stopButton.Enabled = false; studioButton.Enabled = false; outputButton.Enabled = false;
            statusLabel.Text = exit ? "正在停止后台服务并关闭…" : "正在停止后台服务；已下载文件会保留…";
            try
            {
                if (!String.IsNullOrEmpty(controlUrl))
                {
                    try { await ApiAsync("/api/stop", "POST", null, 2500); } catch { }
                    try { await ApiAsync("/api/shutdown", "POST", null, 2500); } catch { }
                }
                CleanupProcesses();
                while (preparing) await Task.Delay(50);
                AppendLog("已停止。下载进度与已保存作品已保留；现在可以修改目录。");
            }
            finally
            {
                stopping = false; studioUrl = null; activeSettings = null; backendPhase = null; progress.Style = ProgressBarStyle.Continuous; progress.Value = 0;
                if (!exit) { LockSettings(false); outputButton.Enabled = true; statusLabel.Text = "已停止 · 修改目录后可重新启动。更换位置不会自动搬移旧文件。"; }
            }
        }
        private void CleanupProcesses()
        {
            if (job != null) { job.Stop(); job.Dispose(); job = null; }
            if (bootstrap != null)
            {
                try { if (!bootstrap.HasExited) KillProcessTree(bootstrap); } catch (InvalidOperationException) { }
                bootstrap.Dispose(); bootstrap = null;
            }
            controlUrl = null; controlToken = null; studioUrl = null; studioButton.Enabled = false;
        }
        private static void KillProcessTree(Process process)
        {
            try
            {
                if (process.HasExited) return;
                using (Process killer = Process.Start(new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "taskkill.exe"), "/PID " + process.Id + " /T /F") { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden }))
                { if (killer != null) killer.WaitForExit(2500); }
                if (!process.HasExited) process.Kill();
            }
            catch (Exception) { try { if (!process.HasExited) process.Kill(); } catch { } }
        }
        private async void OnFormClosing(object sender, FormClosingEventArgs e)
        {
            if (closed) return;
            e.Cancel = true;
            if (closing) return;
            closing = true;
            while (stopping) await Task.Delay(50);
            await StopAsync(true);
            closed = true; Close();
        }
    }

    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            if (args.Length == 2 && args[0] == "--self-test") return SelfTest(args[1]);
            if (args.Length == 1 && args[0] == "--self-test-child") { Thread.Sleep(30000); return 0; }
            if (args.Length == 3 && args[0] == "--self-test-tree")
            {
                // Wait for assignment to the test's job before creating a descendant.
                using (EventWaitHandle ready = EventWaitHandle.OpenExisting(args[1]))
                {
                    if (!ready.WaitOne(10000)) return 3;
                    using (Process child = StartHiddenTestProcess("--self-test-child"))
                    { File.WriteAllText(args[2], child.Id.ToString()); Thread.Sleep(30000); }
                }
                return 0;
            }
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            string sid = WindowsIdentity.GetCurrent().User.Value;
            bool acquired;
            using (Mutex mutex = new Mutex(true, "Local\\MOSS-Image-Desktop-" + sid, out acquired))
            {
                if (!acquired) { MessageBox.Show("MOSS-Image 已经运行。请在任务栏打开现有窗口。", "MOSS-Image", MessageBoxButtons.OK, MessageBoxIcon.Information); return 0; }
                try { Application.Run(new MainForm()); return 0; }
                catch (Exception exc) { MessageBox.Show("软件未能启动：" + exc.Message, "MOSS-Image", MessageBoxButtons.OK, MessageBoxIcon.Error); return 1; }
                finally { mutex.ReleaseMutex(); }
            }
        }

        private static Process StartHiddenTestProcess(string arguments)
        {
            return Process.Start(new ProcessStartInfo(Assembly.GetExecutingAssembly().Location, arguments) {
                UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden
            });
        }

        private static void VerifyJobTree(string temp)
        {
            string eventName = "Local\\MossDesktopSelfTest-" + Guid.NewGuid().ToString("N");
            string pidFile = Path.Combine(temp, "grandchild.pid");
            Process parent = null, grandchild = null;
            ProcessJob job = null;
            using (EventWaitHandle ready = new EventWaitHandle(false, EventResetMode.ManualReset, eventName))
            {
                try
                {
                    job = new ProcessJob();
                    parent = StartHiddenTestProcess("--self-test-tree " + AppFiles.QuoteArgument(eventName) + " " + AppFiles.QuoteArgument(pidFile));
                    job.Add(parent); ready.Set();
                    Stopwatch wait = Stopwatch.StartNew();
                    while (!File.Exists(pidFile) && !parent.HasExited && wait.ElapsedMilliseconds < 5000) Thread.Sleep(25);
                    if (!File.Exists(pidFile)) throw new Exception("Hidden test child did not create its descendant.");
                    int pid = 0;
                    while (wait.ElapsedMilliseconds < 5000)
                    {
                        try { if (Int32.TryParse(File.ReadAllText(pidFile), out pid)) break; } catch (IOException) { }
                        Thread.Sleep(25);
                    }
                    if (pid == 0) throw new Exception("Hidden test descendant did not report its process ID.");
                    grandchild = Process.GetProcessById(pid);
                    // Opening Handle before close also keeps the process identity stable.
                    IntPtr verifiedHandle = grandchild.Handle;
                    job.Dispose(); job = null;
                    if (!parent.WaitForExit(5000) || !grandchild.WaitForExit(5000)) throw new Exception("Job close did not stop the owned process tree.");
                }
                finally
                {
                    if (job != null) job.Dispose();
                    if (grandchild != null) { try { if (!grandchild.HasExited) grandchild.Kill(); } catch { } grandchild.Dispose(); }
                    if (parent != null) { try { if (!parent.HasExited) parent.Kill(); } catch { } parent.Dispose(); }
                }
            }
        }

        private static int SelfTest(string report)
        {
            List<string> results = new List<string>();
            string temp = Path.Combine(Path.GetTempPath(), "MossDesktopSelfTest-" + Guid.NewGuid().ToString("N"));
            int code = 0;
            try
            {
                Directory.CreateDirectory(temp);
                Settings settings = new Settings { schema_version = 2, model_dir = Path.Combine(temp, "models"), data_dir = Path.Combine(temp, "data"), output_dir = Path.Combine(temp, "pictures"), source = "modelscope" };
                AppFiles.ValidateSettings(settings, true); results.Add("PASS: Three independent writable directories, including folder creation.");
                AppFiles.SaveSettings(settings, Path.Combine(temp, "settings.json"));
                Settings roundtrip = AppFiles.Json.Deserialize<Settings>(File.ReadAllText(Path.Combine(temp, "settings.json")));
                if (roundtrip.model_dir != settings.model_dir || roundtrip.schema_version != 2) throw new Exception("Settings roundtrip failed.");
                results.Add("PASS: UTF-8 settings save and roundtrip.");
                string[] invalid = new string[] { "relative\\models", "C:models", "C:\\", "\\\\server\\share", "C:\\Moss\\NUL", "C:\\Moss\\bad?name", Environment.SystemDirectory, AppFiles.Home };
                foreach (string path in invalid)
                {
                    bool rejected = false; try { AppFiles.NormalizeDirectory(path); } catch (ArgumentException) { rejected = true; }
                    if (!rejected) throw new Exception("Accepted unsafe directory: " + path);
                }
                results.Add("PASS: Reject relative, drive root, UNC, device, malformed and protected paths.");
                settings.output_dir = Path.Combine(settings.data_dir, "nested");
                bool overlapRejected = false; try { AppFiles.ValidateSettings(settings, false); } catch (ArgumentException) { overlapRejected = true; }
                if (!overlapRejected) throw new Exception("Overlapping paths were accepted.");
                results.Add("PASS: Reject equal/nested storage directories.");
                string quoted = AppFiles.QuoteArgument("C:\\中文 文件夹\\");
                if (quoted != "\"C:\\中文 文件夹\\\\\"") throw new Exception("Argument quoting failed.");
                results.Add("PASS: Windows argument quoting handles Unicode, spaces and trailing backslash.");
                string extracted = AppFiles.ExtractPayload(Path.Combine(temp, "payload"));
                if (!File.Exists(Path.Combine(extracted, "model-manifest.json"))) throw new Exception("Payload model manifest absent.");
                if (AppFiles.ExtractPayload(extracted) != extracted) throw new Exception("Payload reuse failed.");
                File.AppendAllText(Path.Combine(extracted, "app", "bootstrap.ps1"), "\n# self-test corruption\n");
                AppFiles.ExtractPayload(extracted);
                if (File.ReadAllText(Path.Combine(extracted, "app", "bootstrap.ps1")).Contains("# self-test corruption")) throw new Exception("Payload repair failed.");
                results.Add("PASS: Embedded MossPayload.zip extracts, validates every manifest file, reuses and repairs a changed payload.");
                VerifyJobTree(temp);
                results.Add("PASS: Closing the Windows process job terminates both its hidden child and grandchild; no installation or network access.");
                Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
                using (MainForm form = new MainForm())
                {
                    // Native controls need window handles before DrawToBitmap can render them.
                    // Keep the self-test window entirely invisible and outside the desktop.
                    form.ShowInTaskbar = false; form.StartPosition = FormStartPosition.Manual;
                    form.Location = new Point(-32000, -32000); form.Opacity = 0;
                    form.Show(); form.PerformLayout(); Application.DoEvents();
                    form.RenderSelfTests(report, results);
                    using (Bitmap preview = new Bitmap(form.Width, form.Height))
                    {
                        form.DrawToBitmap(preview, new Rectangle(Point.Empty, form.Size));
                        preview.Save(Path.ChangeExtension(Path.GetFullPath(report), ".ui.png"), System.Drawing.Imaging.ImageFormat.Png);
                    }
                }
                results.Add("PASS: Desktop form constructs and renders offscreen without installation or downloads.");
            }
            catch (Exception exc) { code = 1; results.Add("FAIL: " + exc); }
            finally { try { if (Directory.Exists(temp)) Directory.Delete(temp, true); } catch { } }
            try { File.WriteAllLines(Path.GetFullPath(report), results, new UTF8Encoding(false)); } catch { return 2; }
            return code;
        }
    }
}
