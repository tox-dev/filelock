#pylint: skip-file

from filelock import FileLock
from time import sleep
import logging
import threading

logging.basicConfig(level=logging.DEBUG,
                    format='[%(asctime)s] {%(process)d} %(threadName)s(%(thread)s): %(message)s',
                    # datefmt='%H:%M:%S'
                    )

LOCK = FileLock('bjorn.lock')
PROCESS_LOCK = FileLock('process.lock')
COLOR_LOCK = FileLock('color.lock')

LOG_COLORS = ['\033[031m',
              '\033[032m',
              '\033[033m',
              '\033[034m',
              '\033[035m',
              '\033[036m',
              '\033[037m',
              '\033[41m',
              '\033[42m',
              '\033[43m',
              '\033[44m',
              '\033[45m',
              '\033[46m',
              '\033[47m'
]

ENDC = '\033[0m'

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

def main():
    process_idx = get_process_idx()
    thread = threading.currentThread().getName()
    log_color = get_color()


    while True:

        print log_color

        # logging.debug("App %s, %s: attempting to acquire LOCK the first time", process_idx, thread)
        LOCK.acquire()
        logging.debug("App %s, %s: acquired LOCK the first time", process_idx, thread)

        # logging.debug("  App %s, %s: attempting to acquire LOCK a second time", process_idx, thread)
        LOCK.acquire()
        logging.debug("  App %s, %s: acquired LOCK a second time", process_idx, thread)

        # logging.debug("    App %s, %s: attempting to acquire LOCK a third time", process_idx, thread)
        LOCK.acquire()
        logging.debug("    App %s, %s: acquired LOCK a third time", process_idx, thread)

        LOCK.release()
        logging.debug("    App %s, %s: released 3rd LOCK", process_idx, thread)

        LOCK.release()
        logging.debug("  App %s, %s: released 2nd LOCK", process_idx, thread)
        sleep(4)

        LOCK.release()
        logging.debug("App %s, %s: 1st LOCK released (for real now)", process_idx, thread)
        print ENDC + ""
        sleep(.5)

if __name__ == "__main__":
    try:
        main()
    except (StandardError, KeyboardInterrupt):
        logging.exception('Exception ocurred while running main(). Quitting application.')
