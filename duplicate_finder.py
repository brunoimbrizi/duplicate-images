#!/usr/bin/env python3
"""
A tool to find and remove duplicate pictures.

Usage:
    duplicate_finder.py add <path> ... [--db=<db_path>] [--parallel=<num_processes>] [--hash-size=<power_of_2>]
    duplicate_finder.py remove <path> ... [--db=<db_path>]
    duplicate_finder.py clear [--db=<db_path>]
    duplicate_finder.py show [--db=<db_path>]
    duplicate_finder.py find [--print] [--delete] [--match-time] [--trash=<trash_path>] [--db=<db_path>]
    duplicate_finder.py -h | --help

Options:
    -h, --help                Show this screen

    --db=<db_path>            The location of the database or a MongoDB URI. (default: ./db)

    --parallel=<num_processes> The number of parallel processes to run to hash the image
                               files (default: number of CPUs).
    
    --hash-size=<power_of_2>  ImageHash pHash size (default: 8)

    find:
        --print               Only print duplicate files rather than displaying HTML file
        --delete              Move all found duplicate pictures to the trash. This option takes priority over --print.
        --match-time          Adds the extra constraint that duplicate images must have the
                              same capture times in order to be considered.
        --trash=<trash_path>  Where files will be put when they are deleted (default: ./Trash)
"""

import concurrent.futures
from contextlib import contextmanager
import os
import magic
import math
from pprint import pprint
import shutil
from subprocess import Popen, PIPE, TimeoutExpired
from tempfile import TemporaryDirectory
import webbrowser

from flask import Flask
from flask_cors import CORS
import imagehash
from jinja2 import FileSystemLoader, Environment
from more_itertools import chunked
from itertools import repeat
from PIL import Image, ExifTags
import pymongo
from termcolor import cprint


@contextmanager
def connect_to_db(db_conn_string='./db'):
    p = None

    # Determine db_conn_string is a mongo URI or a path
    # If this is a URI
    if 'mongodb://' == db_conn_string[:10] or 'mongodb+srv://' == db_conn_string[:14]:
        client = pymongo.MongoClient(db_conn_string)
        cprint("Connected server...", "yellow")
        db = client.image_database
        images = db.images

    # If this is not a URI
    else:
        if not os.path.isdir(db_conn_string):
            os.makedirs(db_conn_string)

        p = Popen(['mongod', '--dbpath', db_conn_string], stdout=PIPE, stderr=PIPE, shell=True)

        try:
            p.wait(timeout=2)
            stdout, stderr = p.communicate()
            cprint("Error starting mongod", "red")
            cprint(stdout.decode(), "red")
            exit()
        except TimeoutExpired:
            pass

        cprint("Started database...", "yellow")
        client = pymongo.MongoClient()
        db = client.image_database
        images = db.images

    yield images

    client.close()

    if p is not None:
        cprint("Stopped database...", "yellow")
        p.terminate()


def get_image_files(path):
    """
    Check path recursively for files. If any compatible file is found, it is
    yielded with its full path.

    :param path:
    :return: yield absolute path
    """
    def is_image(file_name):
        # List mime types fully supported by Pillow
        full_supported_formats = ['gif', 'jp2', 'jpeg', 'pcx', 'png', 'tiff', 'x-ms-bmp',
                                  'x-portable-pixmap', 'x-xbitmap']
        try:
            mime = magic.from_file(file_name, mime=True)
            return mime.rsplit('/', 1)[1] in full_supported_formats
        except IndexError:
            return False

    path = os.path.abspath(path)
    for root, dirs, files in os.walk(path):
        for file in files:
            file = os.path.join(root, file)
            if is_image(file):
                yield file


def hash_file(file, hash_size=8):
    try:
        hashes = []
        img = Image.open(file)

        file_size = get_file_size(file)
        image_size = get_image_size(img)
        capture_time = get_capture_time(img)

        # hash the image 4 times and rotate it by 90 degrees each time
        for angle in [ 0, 90, 180, 270 ]:
            if angle > 0:
                turned_img = img.rotate(angle, expand=True)
            else:
                turned_img = img
            hashes.append(str(imagehash.phash(turned_img, hash_size)))

        hashes = ''.join(sorted(hashes))

        cprint("\tHashed {}".format(file), "blue")
        return file, hashes, file_size, image_size, capture_time
    except OSError:
        cprint("\tUnable to open {}".format(file), "red")
        return None


def hash_files_parallel(files, num_processes=None):
    with concurrent.futures.ProcessPoolExecutor(max_workers=num_processes) as executor:
        for result in executor.map(hash_file, files, repeat(HASH_SIZE)):
            if result is not None:
                yield result


def _add_to_database(file_, hash_, file_size, image_size, capture_time, db):
    try:
        db.insert_one({"_id": file_,
                       "hash": hash_,
                       "file_size": file_size,
                       "image_size": image_size,
                       "capture_time": capture_time})
    except pymongo.errors.DuplicateKeyError:
        cprint("Duplicate key: {}".format(file_), "red")


def _in_database(file, db):
    return db.count({"_id": file}) > 0


def new_image_files(files, db):
    for file in files:
        if _in_database(file, db):
            cprint("\tAlready hashed {}".format(file), "green")
        else:
            yield file


def add(paths, db, num_processes=None):
    for path in paths:
        cprint("Hashing {}".format(path), "blue")
        files = get_image_files(path)
        files = new_image_files(files, db)

        for result in hash_files_parallel(files, num_processes):
            _add_to_database(*result, db=db)

        cprint("...done", "blue")


def remove(paths, db):
    for path in paths:
        files = get_image_files(path)

        # TODO: Can I do a bulk delete?
        for file in files:
            remove_image(file, db)


def remove_image(file, db):
    db.delete_one({'_id': file})


def clear(db):
    db.drop()


def show(db):
    total = db.count()
    pprint(list(db.find()))
    print("Total: {}".format(total))


def same_time(dup):
    items = dup['items']
    if "Time unknown" in items:
        # Since we can't know for sure, better safe than sorry
        return True

    if len(set([i['capture_time'] for i in items])) > 1:
        return False

    return True


def find(db, match_time=False):
    dups = db.aggregate([{
        "$group": {
            "_id": "$hash",
            "total": {"$sum": 1},
            "items": {
                "$push": {
                    "file_name": "$_id",
                    "file_size": "$file_size",
                    "image_size": "$image_size",
                    "capture_time": "$capture_time"
                }
            }
        }
    },
    {
        "$match": {
            "total": {"$gt": 1}
        }
    }])

    if match_time:
        dups = (d for d in dups if same_time(d))

    return list(dups)


def delete_duplicates(duplicates, db):
    results = [delete_picture(x['file_name'], db)
               for dup in duplicates for x in dup['items'][1:]]
    cprint("Deleted {}/{} files".format(results.count(True),
                                        len(results)), 'yellow')


def delete_picture(file_name, db, trash="./Trash/"):
    cprint("Moving {} to {}".format(file_name, trash), 'yellow')
    if not os.path.exists(trash):
        os.makedirs(trash)
    try:
        shutil.move(file_name, trash + os.path.basename(file_name))
        remove_image(file_name, db)
    except FileNotFoundError:
        cprint("File not found {}".format(file_name), 'red')
        return False
    except Exception as e:
        cprint("Error: {}".format(str(e)), 'red')
        return False

    return True


def display_duplicates(duplicates, db, trash="./Trash/"):
    from werkzeug.routing import PathConverter
    class EverythingConverter(PathConverter):
        regex = '.*?'

    app = Flask(__name__)
    CORS(app)
    app.url_map.converters['everything'] = EverythingConverter

    def render(duplicates, current, total):
        env = Environment(loader=FileSystemLoader('template'))
        template = env.get_template('index.html')
        return template.render(duplicates=duplicates,
                               current=current,
                               total=total)

    with TemporaryDirectory() as folder:
        # Generate all of the HTML files
        chunk_size = 25
        for i, dups in enumerate(chunked(duplicates, chunk_size)):
            with open('{}/{}.html'.format(folder, i), 'w') as f:
                f.write(render(dups,
                               current=i,
                               total=math.ceil(len(duplicates) / chunk_size)))

        webbrowser.open("file://{}/{}".format(folder, '0.html'))

        @app.route('/picture/<everything:file_name>', methods=['DELETE'])
        def delete_picture_(file_name, trash=trash):
            return str(delete_picture(file_name, db, trash))

        app.run()


def get_file_size(file_name):
    try:
        return os.path.getsize(file_name)
    except FileNotFoundError:
        return 0


def get_image_size(img):
    return "{} x {}".format(*img.size)


def get_capture_time(img):
    try:
        exif = {
            ExifTags.TAGS[k]: v
            for k, v in img._getexif().items()
            if k in ExifTags.TAGS
        }
        return exif["DateTimeOriginal"]
    except:
        return "Time unknown"

def next_power_of_2(x):
    return 1 if x == 0 else 2**math.ceil(math.log2(x))

if __name__ == '__main__':
    from docopt import docopt
    args = docopt(__doc__)

    if args['--trash']:
        TRASH = args['--trash']
    else:
        TRASH = "./Trash/"

    if args['--db']:
        DB_PATH = args['--db']
    else:
        DB_PATH = "./db"

    if args['--parallel']:
        NUM_PROCESSES = int(args['--parallel'])
    else:
        NUM_PROCESSES = None

    if args['--hash-size']:
        HASH_SIZE = next_power_of_2(int(args['--hash-size']))
    else:
        HASH_SIZE = 8

    with connect_to_db(db_conn_string=DB_PATH) as db:
        if args['add']:
            add(args['<path>'], db, NUM_PROCESSES)
        elif args['remove']:
            remove(args['<path>'], db)
        elif args['clear']:
            clear(db)
        elif args['show']:
            show(db)
        elif args['find']:
            dups = find(db, args['--match-time'])

            if args['--delete']:
                delete_duplicates(dups, db)
            elif args['--print']:
                pprint(dups)
                print("Number of duplicates: {}".format(len(dups)))
            else:
                display_duplicates(dups, db=db)
