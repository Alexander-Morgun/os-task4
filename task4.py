﻿import argparse
from multiprocessing import Process, Lock
from ctypes import *
from multiprocessing.sharedctypes import Array as SharedArray,
Value as SharedValue
import cmd
from time import localtime, time, strftime
from io import BytesIO
import zlib
from struct import pack

parser = argparse.ArgumentParser(description=' Copyright (C) Alexander Morgun'
                                 ' <Alexander_Morgun@e1.ru> WTFPL')
parser.add_argument('file', default="", help='File with image of file system')
parser.add_argument('-n', action='store_true',
                    help='Create new image of file system in specified file')
args = parser.parse_args()

n_segments = 65536
segment_len = 1024
n_threads = 3


class FileRecord(Structure):
    _fields_ = [('used', c_bool),
                ('name', c_char * 64),
                ('is_first', c_bool),
                ('last_modified', c_double),
                ('is_last', c_bool),
                ('next_segment', c_size_t),
                ('size', c_size_t),
                ]


class Segment(Structure):
    _fields_ = [
        ('data', c_ubyte * segment_len),
    ]


class FileSystem(Structure):
    _fields_ = [
        ('file_records', FileRecord * n_segments),
        ('segments', Segment * n_segments),
    ]


def get_empty_segment(fs):
    for i in range(n_segments):
        if not fs.file_records[i].used:
            return i
    return -1


def find_file(fs, file):
    name = bytes(file, 'utf8')
    for i in range(n_segments):
        if fs.file_records[i].is_first and \
            fs.file_records[i].used and \
                fs.file_records[i].name == name:
            return i
    return -1


def remove_file(fs, file_id):
    if fs.file_records[file_id].used:
        fs.file_records[file_id].used = False
        if not fs.file_records[file_id].is_last:
            remove_file(fs, fs.file_records[file_id].next_segment)


def create_file(fs, file):
    seg = find_file(fs, file)
    if seg >= 0:
        remove_file(fs, seg)
    else:
        seg = get_empty_segment(fs)
    if seg >= 0:
        fs.file_records[seg].used = True
        fs.file_records[seg].is_first = True
        fs.file_records[seg].is_last = True
        fs.file_records[seg].name = bytes(file, 'utf8')
        fs.file_records[seg].last_modified = time()
        fs.file_records[seg].size = 0
    return seg


def write_bytes_to_file(fs, file_id, to_write):
    seg = file_id
    fs.file_records[file_id].last_modified = time()
    while not fs.file_records[seg].is_last:
        seg = fs.file_records[seg].next_segment
    while len(to_write):
        offset = fs.file_records[file_id].size % segment_len
        diff = min(len(to_write), segment_len - offset)
        for num, b in enumerate(to_write[:diff]):
            fs.segments[seg].data[offset + num] = b
        fs.file_records[file_id].size += diff
        to_write = to_write[diff:]
        if len(to_write):
            new_seg = get_empty_segment(fs)
            if new_seg == -1:
                return -1
            fs.file_records[seg].next_segment = new_seg
            fs.file_records[seg].is_last = False
            fs.file_records[new_seg].used = True
            fs.file_records[new_seg].is_first = False
            fs.file_records[new_seg].is_last = True
            seg = new_seg
    return 0


def tester(fs, my_number, level, waiting):
    oth_number = my_number ^ 1
    for i in fs.file_records:
        for l in range(1, n_threads):
            level[my_number] = c_int(l)
            waiting[l] = c_int(my_number)
            while True:
                if any([level[k] >= l for k in range(n_threads)
                        if k != my_number]) and waiting[l] == my_number:
                    continue
                break
        if i.is_first and i.used:
            print("%s %d" % (str(i.name, 'utf8'), i.size))
        level[my_number] = c_int(0)


if __name__ == '__main__':

    level = SharedArray(c_int, [0 for i in range(n_threads)], lock=False)
    waiting = SharedArray(c_int, [0 for i in range(n_threads)], lock=False)

    class MyShell(cmd.Cmd):
        intro = 'Welcome to my file system shell. Type help or ? to list commands.\n'
        prompt = '>>> '

        def do_exit(self, arg):
            'Exit from this shell'
            return True

        def do_ls(self, arg):
            'List information about files'
            print("% 20s % 10s % -20s" % ('Modified', 'Size', 'Name', ))
            for i in range(n_segments):
                if fs.file_records[i].is_first and \
                        fs.file_records[i].used:
                    name = str(fs.file_records[i].name, 'utf8')
                    print("% 20s % 10s %s" %
                          (strftime("%d.%m.%Y %H:%M:%S",
                                    localtime(fs.file_records[i].last_modified)),
                           fs.file_records[i].size,
                           name))

        def do_touch(self, arg):
            'Create new file with specified name or update existing one'
            arg = arg[:64]
            if not arg:
                print('Missing argument')
                return
            file = find_file(fs, arg)
            if file == -1:
                create_file(fs, arg)
            else:
                fs.file_records[file].last_modified = time()

        def do_rm(self, arg):
            'Remove file'
            if not arg:
                print('Missing argument')
                return
            file = find_file(fs, arg)
            if file == -1:
                print("Cannot remove %s : No such file" % arg)
            else:
                remove_file(fs, file)

        def do_export(self, arg):
            'Export file from file system'
            if not arg:
                print('Missing argument')
                return
            try:
                arg = arg[:64]
                file = find_file(fs, arg)
                if file == -1:
                    print("%s : No such file" % arg)
                else:
                    file_name = input('Enter destination file:')
                    f = open(file_name, 'wb')
                    curr_len = fs.file_records[file].size
                    while curr_len:
                        diff = min(curr_len, segment_len)
                        part = pack('B' * diff, *fs.segments[file].data[:diff])
                        f.write(part)
                        file = fs.file_records[file].next_segment
                        curr_len -= diff
                    f.close()
            except Exception as e:
                print(e)

        def do_import(self, arg):
            'Import external file to file system. Sample: import C:\WINDOWS\explorer.exe'
            if not arg:
                print('Missing argument')
                return
            try:
                f = open(arg, 'rb')
                file_name = input('Enter name of file in file system:')[:64]
                file = create_file(fs, file_name)
                if file == -1:
                    file = create_file(fs, file_name)
                write_bytes_to_file(fs, file, f.read())
                f.close()
            except Exception as e:
                print(e)

        def do_cat(self, arg):
            'Print file on the standard output'
            if arg.find('>>') >= 0:
                command = arg[:arg.find('>>')]
                file_name = arg[arg.find('>>') + 2:][:64]
                if not file_name:
                    print('Empty name of file')
                    return
                file = find_file(fs, file_name)
                if file == -1:
                    file = create_file(fs, arg[arg.find('>>') + 2:])
            elif arg.find('>') >= 0:
                command = arg[:arg.find('>')]
                file_name = arg[arg.find('>') + 1:][:64]
                if not file_name:
                    print('Empty name of file')
                    return
                file = create_file(fs, file_name)
            else:
                command = arg
                file = -1
            data = b""
            if command:
                i_file = find_file(fs, command)
                if i_file == -1:
                    print("%s : No such file" % arg)
                else:
                    curr_len = fs.file_records[i_file].size
                    while curr_len:
                        diff = min(curr_len, segment_len)
                        data += pack(
                            'B' * diff, *fs.segments[i_file].data[:diff])
                        i_file = fs.file_records[i_file].next_segment
                        curr_len -= diff
            else:
                print("Use Ctrl + C to end input")
                try:
                    data = ""
                    while True:
                        data += input() + '\n'
                except:
                    data = bytes(data, 'utf8')
            if file == -1:
                print(str(data, 'utf8'))
            else:
                write_bytes_to_file(fs, file, data)
            return

        def do_run_test(self, arg):
            'Create two threads that print sizes of all files'
            p = [Process(target=tester, args=(fs, i, level, waiting))
                 for i in range(n_threads)]
            for i in p:
                i.start()
            for i in p:
                i.join()

    fs = SharedValue(FileSystem, lock=False)
    f = open(args.file, 'rb')
    bytes_all = f.read()
    if args.n:
        img_bytes = bytes_all
    else:
        data_len = bytes_all[-sizeof(c_size_t):]
        fs_size = int.from_bytes(
            data_len, byteorder='little', signed=False) + sizeof(c_size_t)
        img_bytes = bytes_all[:-fs_size]
        fs_bytes = zlib.decompress(bytes_all[-fs_size:-sizeof(c_size_t)])
        fakefile = BytesIO(fs_bytes)
        fakefile.readinto(fs)
    f.close()
    try:
        MyShell().cmdloop()
    except:
        pass

    f = open(args.file, 'wb')
    f.write(img_bytes)
    fakefile = BytesIO()
    fakefile.write(fs)
    fs_bytes = zlib.compress(fakefile.getvalue())
    f.write(fs_bytes)
    fakefile = BytesIO()
    fakefile.write(c_size_t(len(fs_bytes)))
    f.write(c_size_t(len(fs_bytes)))
