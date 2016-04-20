#pylint: skip-file

# Try testing with this:
#
#    python app1.py & python app1.py & python app1.py & python app1.py &

from filelock import FileLock
from time import sleep
import threading
import logging
import os

logging.basicConfig(level=logging.DEBUG,
                    format='[%(asctime)s] {%(process)d} %(threadName)s(%(thread)s): %(message)s',
                    # datefmt='%H:%M:%S',
                    )

LOCK = FileLock('bjorn.lock')
PROCESS_LOCK = FileLock('process.lock')
COLOR_LOCK = FileLock('color.lock')

LOG_COLORS = [
    '\033[031m',  # Red
    '\033[032m',  # Green
    '\033[033m',  # Yellow
    '\033[034m',  # Blue
    '\033[035m',  # Purple
    '\033[036m',  # Cyan
    '\033[037m',  # White
    '\033[41m',   # Red background
    '\033[42m',   # Green background
    '\033[43m',   # Yellow background
    '\033[44m',   # Blue background
    '\033[45m',   # Purple background
    '\033[46m',   # Cyan background
    '\033[47m'    # White background
]

ENDC = '\033[0m'

def worker(stop_event, process_idx, log_color):

    thread = threading.currentThread().getName()

    while not stop_event.is_set():
        # logging.debug("App %s, %s: attempting to acquire LOCK the first time", process_idx, thread)

        with LOCK:
            print log_color

            logging.debug("App %s, %s: acquired LOCK the first time", process_idx, thread)

            # logging.debug("  App %s, %s: attempting to acquire LOCK a second time", process_idx, thread)

            with LOCK:
                logging.debug("  App %s, %s: acquired LOCK a second time", process_idx, thread)
                # logging.debug("    App %s, %s: attempting to acquire LOCK a third time", process_idx, thread)

                with LOCK:
                    logging.debug("    App %s, %s: acquired LOCK a third time", process_idx, thread)

                logging.debug("    App %s, %s: released 3rd LOCK", process_idx, thread)

            logging.debug("  App %s, %s: released 2nd LOCK", process_idx, thread)
            # sleep(1)

        logging.debug("App %s, %s: 1st LOCK released (for real now)", process_idx, thread)
        print ENDC + ""

        sleep(.5)

def get_process_idx():
    with PROCESS_LOCK:

        with open('process-idx.txt') as f:
            process_idx = int(f.readlines()[0])

        result = process_idx

        process_idx += 1

        with open('process-idx.txt', 'w+') as f:
            f.writelines([str(process_idx)])

        return result


def get_color():
    with COLOR_LOCK:

        with open('log-color-idx.txt') as f:
            color_idx = int(f.readlines()[0])

        result = color_idx

        color_idx += 1
        color_idx %= len(LOG_COLORS)

        with open('log-color-idx.txt', 'w+') as f:
            f.writelines([str(color_idx)])

        return LOG_COLORS[result]

def create_process_and_color_idx_files_if_needed():
    try:
        with PROCESS_LOCK:
            with open('process-idx.txt') as f:
                pass
    except IOError:
        with open('process-idx.txt', 'w+') as f:
            f.write('0')

    try:
        with COLOR_LOCK:
            with open('log-color-idx.txt') as f:
                pass
    except IOError:
        with open('log-color-idx.txt', 'w+') as f:
            f.write('0')


def main(thread_stopper):
    create_process_and_color_idx_files_if_needed()

    threads = []
    process_idx = get_process_idx()

    for i in range(3):
        log_color = get_color()
        job = threading.Thread(target=worker, name='thread %d' % i, args=(thread_stopper, process_idx, log_color))
        threads.append(job)
        job.start()

    while not thread_stopper.is_set():
        sleep(.1)

if __name__ == "__main__":
    thread_stopper = threading.Event()

    try:
        main(thread_stopper)
    except (StandardError, KeyboardInterrupt):
        logging.exception('Exception ocurred while running main(). Quitting application.')
        # thread_stopper.set()
    finally:
        os._exit(0)
