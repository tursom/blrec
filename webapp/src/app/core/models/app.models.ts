export interface AppInfo {
  name: string;
  version: string;
  pid: number;
  ppid: number;
  /** 进程创建时间，Unix 时间戳（秒）。 */
  create_time: number;
  cwd: string;
  exe: string;
  cmdline: string[];
}

export interface appStatus {
  cpu_percent: number;
  memory_percent: number;
  num_threads: number;
}
