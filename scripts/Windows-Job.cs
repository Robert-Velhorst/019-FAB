using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace Fab.Windows
{
    // Keep C# 5 compatibility for the Windows PowerShell 5.1 compiler.
    public sealed class JobLease : IDisposable
    {
        public const string SourceHash = "__FAB_JOB_HASH__";
        private SafeFileHandle job;
        private bool committed;
        public Process Process { get; private set; }
        public string JobName { get; private set; }

        private JobLease(SafeFileHandle handle, Process process, string name)
        {
            job = handle;
            Process = process;
            JobName = name;
        }

        public static JobLease Open(string name, Process process)
        {
            Guid identifier;
            if (name == null || !name.StartsWith("Local\\FAB-", StringComparison.Ordinal) ||
                !Guid.TryParseExact(name.Substring(10), "N", out identifier))
                throw new ArgumentException("Invalid FAB service job identity.");
            SafeFileHandle handle = OpenJobObject(0xC, false, name); // QUERY | TERMINATE
            if (handle.IsInvalid)
            {
                int error = Marshal.GetLastWin32Error();
                handle.Dispose();
                if (error == 2) return null; // The original named job is gone.
                throw new Win32Exception(error, "FAB failed to open service job.");
            }
            bool transferred = false;
            try
            {
                Check(!handle.IsInvalid, "open service job");
                if (process != null)
                {
                    bool member;
                    Check(IsProcessInJob(process.Handle, handle, out member), "verify service job membership");
                    if (!member) throw new InvalidOperationException("The process does not belong to the recorded FAB job.");
                }
                JobLease lease = new JobLease(handle, process, name);
                transferred = true;
                return lease;
            }
            finally { if (!transferred) handle.Dispose(); }
        }

        public void Commit()
        {
            if (committed) return;
            if (job.IsClosed) throw new ObjectDisposedException("JobLease");
            IntPtr keeper;
            // Only the original service root retains the job lifetime. The handle
            // grants no management rights and cannot be inherited by descendants.
            Check(DuplicateHandle(GetCurrentProcess(), job, Process.Handle,
                out keeper, 0, false, 0), "retain service job");
            committed = true;
        }

        public uint ActiveProcessCount
        {
            get
            {
                Accounting info;
                Check(QueryInformationJobObject(job, 1, out info,
                    (uint)Marshal.SizeOf(typeof(Accounting)), IntPtr.Zero), "query service job");
                return info.ActiveProcesses;
            }
        }

        public void Terminate()
        {
            Check(TerminateJobObject(job, 1), "terminate service job");
            Stopwatch watch = Stopwatch.StartNew();
            while (ActiveProcessCount != 0)
            {
                if (watch.ElapsedMilliseconds >= 5000)
                    throw new TimeoutException("FAB could not verify complete service job cleanup.");
                Thread.Sleep(10);
            }
        }

        public void Dispose() { job.Dispose(); }

        public static JobLease Start(string executable, string[] arguments,
            string directory, string stdout, string stderr)
        {
            if (!Path.IsPathRooted(executable) || !File.Exists(executable) ||
                !String.Equals(Path.GetExtension(executable), ".exe", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("FAB requires an existing absolute executable path.");
            if (!Directory.Exists(directory)) throw new DirectoryNotFoundException("FAB service directory is missing.");
            if (String.Equals(Path.GetFullPath(stdout), Path.GetFullPath(stderr), StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("FAB stdout and stderr paths must differ.");
            StringBuilder command = new StringBuilder(Quote(executable));
            foreach (string argument in arguments) command.Append(' ').Append(Quote(argument));
            if (command.Length >= 32767) throw new ArgumentException("FAB service command is too long.");

            string jobName = "Local\\FAB-" + Guid.NewGuid().ToString("N");
            SafeFileHandle handle = CreateJobObject(IntPtr.Zero, jobName);
            Process process = null;
            ProcessInformation pi = new ProcessInformation();
            bool transferred = false;
            IntPtr attributes = IntPtr.Zero;
            IntPtr handles = IntPtr.Zero;
            IntPtr jobs = IntPtr.Zero;
            bool initialized = false;
            try
            {
                Check(!handle.IsInvalid, "create service job");
                ExtendedLimits limits = new ExtendedLimits();
                limits.Basic.LimitFlags = 0x2000; // KILL_ON_JOB_CLOSE, no breakaway.
                Check(SetInformationJobObject(handle, 9, ref limits,
                    (uint)Marshal.SizeOf(typeof(ExtendedLimits))), "configure service job");
                SecurityAttributes security = new SecurityAttributes();
                security.Length = Marshal.SizeOf(typeof(SecurityAttributes));
                security.InheritHandle = true;
                using (SafeFileHandle input = CreateFile("NUL", 0x80000000, 7, ref security, 3, 0, IntPtr.Zero))
                using (SafeFileHandle output = CreateFile(stdout, 0x40000000, 7, ref security, 2, 0, IntPtr.Zero))
                using (SafeFileHandle error = CreateFile(stderr, 0x40000000, 7, ref security, 2, 0, IntPtr.Zero))
                {
                    Check(!input.IsInvalid && !output.IsInvalid && !error.IsInvalid, "open service logs");
                    IntPtr size = IntPtr.Zero;
                    InitializeProcThreadAttributeList(IntPtr.Zero, 2, 0, ref size);
                    attributes = Marshal.AllocHGlobal(size);
                    Check(InitializeProcThreadAttributeList(attributes, 2, 0, ref size), "initialize service handles");
                    initialized = true;
                    handles = Marshal.AllocHGlobal(IntPtr.Size * 3);
                    Marshal.WriteIntPtr(handles, 0, input.DangerousGetHandle());
                    Marshal.WriteIntPtr(handles, IntPtr.Size, output.DangerousGetHandle());
                    Marshal.WriteIntPtr(handles, IntPtr.Size * 2, error.DangerousGetHandle());
                    Check(UpdateProcThreadAttribute(attributes, 0, new IntPtr(0x20002), handles,
                        new IntPtr(IntPtr.Size * 3), IntPtr.Zero, IntPtr.Zero), "restrict service handle inheritance");
                    jobs = Marshal.AllocHGlobal(IntPtr.Size);
                    Marshal.WriteIntPtr(jobs, handle.DangerousGetHandle());
                    // JOB_LIST makes assignment part of creation on Windows 10+.
                    // A launcher crash cannot strand an unassigned suspended root.
                    Check(UpdateProcThreadAttribute(attributes, 0, new IntPtr(0x2000D), jobs,
                        new IntPtr(IntPtr.Size), IntPtr.Zero, IntPtr.Zero), "configure atomic service containment");
                    StartupInfoEx startup = new StartupInfoEx();
                    startup.Info.Size = Marshal.SizeOf(typeof(StartupInfoEx));
                    startup.Info.Flags = 0x100; // STARTF_USESTDHANDLES
                    startup.Info.Input = input.DangerousGetHandle();
                    startup.Info.Output = output.DangerousGetHandle();
                    startup.Info.Error = error.DangerousGetHandle();
                    startup.Attributes = attributes;
                    // No service code executes until assignment succeeds. Inherit
                    // the caller's scoped environment, never credentials in argv.
                    Check(CreateProcess(executable, command, IntPtr.Zero, IntPtr.Zero, true,
                        0x08080004, IntPtr.Zero, directory, ref startup, out pi), "create suspended service");
                    process = Process.GetProcessById((int)pi.ProcessId);
                    IntPtr retainedHandle = process.Handle;
                    Check(ResumeThread(pi.Thread) != UInt32.MaxValue, "resume service");
                }
                JobLease lease = new JobLease(handle, process, jobName);
                transferred = true;
                return lease;
            }
            finally
            {
                if (!transferred)
                {
                    if (pi.Process != IntPtr.Zero) TerminateProcess(pi.Process, 1);
                    handle.Dispose();
                    if (process != null) process.Dispose();
                }
                if (pi.Thread != IntPtr.Zero) CloseHandle(pi.Thread);
                if (pi.Process != IntPtr.Zero) CloseHandle(pi.Process);
                if (initialized) DeleteProcThreadAttributeList(attributes);
                if (attributes != IntPtr.Zero) Marshal.FreeHGlobal(attributes);
                if (handles != IntPtr.Zero) Marshal.FreeHGlobal(handles);
                if (jobs != IntPtr.Zero) Marshal.FreeHGlobal(jobs);
            }
        }

        private static string Quote(string value)
        {
            if (value == null || value.IndexOf('\0') >= 0)
                throw new ArgumentException("FAB service arguments cannot contain null characters.");
            StringBuilder result = new StringBuilder("\"");
            int slashes = 0;
            foreach (char character in value)
            {
                if (character == '\\') { slashes++; continue; }
                result.Append('\\', character == '"' ? slashes * 2 + 1 : slashes);
                result.Append(character);
                slashes = 0;
            }
            return result.Append('\\', slashes * 2).Append('"').ToString();
        }

        private static void Check(bool success, string operation)
        {
            if (!success) throw new Win32Exception(Marshal.GetLastWin32Error(), "FAB failed to " + operation + ".");
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct BasicLimits
        {
            public long ProcessTime, JobTime;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSet, MaximumWorkingSet;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass, SchedulingClass;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters { public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes; }
        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedLimits
        {
            public BasicLimits Basic;
            public IoCounters Io;
            public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct Accounting
        {
            public long UserTime, KernelTime, PeriodUserTime, PeriodKernelTime;
            public uint PageFaults, TotalProcesses, ActiveProcesses, TerminatedProcesses;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes
        {
            public int Length;
            public IntPtr Descriptor;
            [MarshalAs(UnmanagedType.Bool)] public bool InheritHandle;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct StartupInfo
        {
            public int Size;
            public string Reserved, Desktop, Title;
            public uint X, Y, XSize, YSize, XChars, YChars, FillAttribute, Flags;
            public ushort ShowWindow, ReservedSize;
            public IntPtr ReservedBytes, Input, Output, Error;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct StartupInfoEx { public StartupInfo Info; public IntPtr Attributes; }
        [StructLayout(LayoutKind.Sequential)]
        private struct ProcessInformation { public IntPtr Process, Thread; public uint ProcessId, ThreadId; }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateJobObject(IntPtr security, string name);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle OpenJobObject(uint access, bool inherit, string name);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool IsProcessInJob(IntPtr process, SafeFileHandle job, out bool member);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(SafeFileHandle job, int kind, ref ExtendedLimits limits, uint size);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool QueryInformationJobObject(SafeFileHandle job, int kind, out Accounting info, uint size, IntPtr returned);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateJobObject(SafeFileHandle job, uint exitCode);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool DuplicateHandle(IntPtr source, SafeFileHandle handle, IntPtr target, out IntPtr duplicate, uint access, bool inherit, uint options);
        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(string path, uint access, uint share, ref SecurityAttributes security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, uint flags, ref IntPtr size);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr attribute, IntPtr value, IntPtr size, IntPtr previous, IntPtr returned);
        [DllImport("kernel32.dll")]
        private static extern void DeleteProcThreadAttributeList(IntPtr list);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CreateProcess(string application, StringBuilder command, IntPtr processSecurity, IntPtr threadSecurity, bool inherit, uint flags, IntPtr environment, string directory, ref StartupInfoEx startup, out ProcessInformation process);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr thread);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateProcess(IntPtr process, uint exitCode);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);
    }
}
