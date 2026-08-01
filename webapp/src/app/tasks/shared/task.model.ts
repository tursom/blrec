import { ResponseMessage } from '../../shared/api.models';
import {
  DeleteStrategy,
  StreamFormat,
  QualityNumber,
  RecordingMode,
} from '../../settings/shared/setting.model';

export interface TaskData {
  user_info: Readonly<UserInfo>;
  room_info: Readonly<RoomInfo>;
  task_status: Readonly<TaskStatus>;
}

export enum DataSelection {
  ALL = 'all',

  // live status
  PREPARING = 'preparing',
  LIVING = 'living',
  ROUNDING = 'rounding',

  // task status
  MONITOR_ENABLED = 'monitor_enabled',
  MONITOR_DISABLED = 'monitor_disabled',
  RECORDER_ENABLED = 'recorder_enabled',
  RECORDER_DISABLED = 'recorder_disabled',

  // task running status
  STOPPED = 'stopped',
  WAITTING = 'waitting',
  RECORDING = 'recording',
  REMUXING = 'remuxing',
  INJECTING = 'injecting',
}

export interface UserInfo {
  readonly name: string;
  readonly gender: string;
  readonly face: string;
  readonly uid: number;
  readonly level: number;
  readonly sign: string;
}

export interface RoomInfo {
  readonly uid: number;
  readonly room_id: number;
  readonly short_room_id: number;
  readonly area_id: number;
  readonly area_name: string;
  readonly parent_area_id: number;
  readonly parent_area_name: string;
  readonly live_status: number;
  readonly live_start_time: number;
  readonly online: number;
  readonly title: string;
  readonly cover: string;
  readonly tags: string;
  readonly description: string;
}

export enum RunningStatus {
  STOPPED = 'stopped',
  WAITING = 'waiting',
  RECORDING = 'recording',
  REMUXING = 'remuxing',
  INJECTING = 'injecting',
}

export enum PostprocessorStatus {
  WAITING = 'waiting',
  REMUXING = 'remuxing',
  INJECTING = 'injecting',
}

export interface Progress {
  count: number;
  total: number;
}

export interface TaskStatus {
  readonly monitor_enabled: boolean;
  readonly recorder_enabled: boolean;
  readonly running_status: RunningStatus;
  readonly stream_url: string;
  readonly stream_host: string;
  /** 已下载的媒体字节数。 */
  readonly dl_total: number;
  /** 当前下载速率，单位 byte/s。 */
  readonly dl_rate: number;
  /** 当前录制段已经过的秒数。 */
  readonly rec_elapsed: number;
  /** 当前录制段写入的字节数。 */
  readonly rec_total: number;
  /** 当前文件写入速率，单位 byte/s。 */
  readonly rec_rate: number;
  readonly danmu_total: number;
  readonly danmu_rate: number;
  readonly real_stream_format: StreamFormat | null;
  readonly real_quality_number: QualityNumber | null;
  readonly recording_path: string | null;
  readonly postprocessor_status: PostprocessorStatus;
  readonly postprocessing_path: string | null;
  readonly postprocessing_progress: Progress | null;
}

export interface TaskParam {
  readonly out_dir: string;
  readonly path_template: string;
  /** 单个输出文件的大小上限，单位字节；0 表示不限制。 */
  readonly filesize_limit: number;
  /** 单个输出文件的时长上限，单位秒；0 表示不限制。 */
  readonly duration_limit: number;

  readonly user_agent: string;
  readonly cookie: string;

  readonly danmu_uname: boolean;
  readonly record_gift_send: boolean;
  readonly record_free_gifts: boolean;
  readonly record_guard_buy: boolean;
  readonly record_super_chat: boolean;
  readonly save_raw_danmaku: boolean;
  readonly record_raw_danmaku_during_waiting: boolean;

  readonly stream_format: StreamFormat;
  readonly recording_mode: RecordingMode;
  readonly quality_number: QualityNumber;
  /** 流读取超时，单位秒。 */
  readonly read_timeout: number;
  /** 判定连接中断前允许无数据的秒数。 */
  readonly disconnection_timeout: number;
  /** 媒体读取缓冲区大小，单位字节。 */
  readonly buffer_size: number;
  readonly save_cover: boolean;

  readonly remux_to_mp4: boolean;
  readonly inject_extra_metadata: boolean;
  readonly delete_source: DeleteStrategy;
}

export interface VideoStreamProfile {
  index?: number;
  codec_name?: string;
  codec_long_name?: string;
  profile?: string;
  codec_type?: string;
  codec_tag_string?: string;
  codec_tag?: string;
  width?: number;
  height?: number;
  coded_width?: number;
  coded_height?: number;
  closed_captions?: number;
  film_grain?: number;
  has_b_frames?: number;
  pix_fmt?: string;
  level?: number;
  chroma_location?: string;
  field_order?: string;
  refs?: number;
  is_avc?: string;
  nal_length_size?: string;
  r_frame_rate?: string;
  avg_frame_rate?: string;
  time_base?: string;
  start_pts?: number;
  start_time?: string;
  bit_rate?: string;
  bits_per_raw_sample?: string;
  extradata_size?: number;
  disposition?: {
    default?: number;
    dub?: number;
    original?: number;
    comment?: number;
    lyrics?: number;
    karaoke?: number;
    forced?: number;
    hearing_impaired?: number;
    visual_impaired?: number;
    clean_effects?: number;
    attached_pic?: number;
    timed_thumbnails?: number;
    captions?: number;
    descriptions?: number;
    metadata?: number;
    dependent?: number;
    still_image?: number;
  };
  tags?: {
    language?: string;
    handler_name?: string;
    vendor_id?: string;
    encoder?: string;
  };
}

export interface AudioStreamProfile {
  index?: number;
  codec_name?: string;
  codec_long_name?: string;
  profile?: string;
  codec_type?: string;
  codec_tag_string?: string;
  codec_tag?: string;
  sample_fmt?: string;
  sample_rate?: string;
  channels?: number;
  channel_layout?: string;
  bits_per_sample?: number;
  r_frame_rate?: string;
  avg_frame_rate?: string;
  time_base?: string;
  start_pts?: number;
  start_time?: string;
  duration_ts?: number;
  duration?: string;
  bit_rate?: string;
  extradata_size?: number;
  disposition?: {
    default?: number;
    dub?: number;
    original?: number;
    comment?: number;
    lyrics?: number;
    karaoke?: number;
    forced?: number;
    hearing_impaired?: number;
    visual_impaired?: number;
    clean_effects?: number;
    attached_pic?: number;
    timed_thumbnails?: number;
    captions?: number;
    descriptions?: number;
    metadata?: number;
    dependent?: number;
    still_image?: number;
  };
}

export interface Metadata {
  hasAudio: boolean;
  hasVideo: boolean;
  hasMetadata: boolean;
  hasKeyframes: boolean;
  canSeekToEnd: boolean;
  duration: number;
  datasize: number;
  filesize: number;
  audiosize: number;
  audiocodecid: number;
  audiodatarate: number;
  audiosamplerate: 3;
  audiosamplesize: 1;
  stereo: boolean;
  videosize: number;
  framerate: number;
  videocodecid: 7;
  videodatarate: number;
  width: number;
  height: number;
  lasttimestamp: number;
  lastkeyframelocation: number;
  lastkeyframetimestamp: number;
  keyframes: {
    times: number[];
    filepositions: number[];
  };
}

export interface StreamProfile {
  streams?: [VideoStreamProfile, AudioStreamProfile];
  format?: {
    filename?: string;
    nb_streams?: number;
    nb_programs?: number;
    format_name?: string;
    format_long_name?: string;
    start_time?: string;
    duration?: string;
    probe_score?: number;
    tags?: {
      displayWidth?: string;
      displayHeight?: string;
      fps?: string;
      profile?: string;
      level?: string;
      videocodecreal?: string;
      cdn_ip?: string;
      Server?: string;
      Rawdata?: string;
      encoder?: string;
      string?: string;
      mau?: string;
      major_brand?: string;
      minor_version?: string;
      compatible_brands?: string;
    };
  };
}

export enum VideoFileStatus {
  RECORDING = 'recording',
  REMUXING = 'remuxing',
  INJECTING = 'injecting',
  COMPLETED = 'completed',
  MISSING = 'missing',
  UNKNOWN = 'unknown',
}

export enum DanmakuFileStatus {
  RECORDING = 'recording',
  COMPLETED = 'completed',
  MISSING = 'missing',
  UNKNOWN = 'unknown',
}

export interface VideoFileDetail {
  readonly path: string;
  readonly size: number;
  readonly status: VideoFileStatus;
}

export interface DanmakuFileDetail {
  readonly path: string;
  readonly size: number;
  readonly status: DanmakuFileStatus;
}

export interface AddTaskResult extends ResponseMessage {
  data: {
    room_id: number;
  };
}
