"""Map consecutive exported camera frames onto an offset Blender timeline."""


def export_frame_numbers(count, frame_start_offset=0):
    if type(frame_start_offset) is not int or not 0 <= frame_start_offset <= 10000:
        raise ValueError('导出起始帧偏移需要是 0 到 10000 之间的整数')
    if type(count) is not int or count < 0:
        raise ValueError('相机帧数必须是非负整数')
    first_frame = frame_start_offset + 1
    return list(range(first_frame, first_frame + count))
