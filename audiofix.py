import subprocess
import shlex
import io
import threading
from discord.opus import Encoder
import discord


class FFmpegStreamAudio(discord.AudioSource):
    """Decode an iterator of encoded audio chunks to Discord PCM as they arrive.

    Chunks are fed into ffmpeg's stdin on a background thread while the voice
    player reads decoded PCM frames from stdout, so playback can start before the
    full audio has been downloaded.
    """

    def __init__(self, byte_iterator, *, executable='ffmpeg', before_options=None, options=None):
        args = [executable]
        if isinstance(before_options, str):
            args.extend(shlex.split(before_options))
        args.extend(('-i', '-'))
        args.extend(('-f', 's16le', '-ar', '48000', '-ac', '2', '-loglevel', 'warning'))
        if isinstance(options, str):
            args.extend(shlex.split(options))
        args.append('pipe:1')
        self._process = None
        try:
            self._process = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None
            )
        except FileNotFoundError:
            raise discord.ClientException(executable + ' was not found.') from None
        except subprocess.SubprocessError as exc:
            raise discord.ClientException('Popen failed: {0.__class__.__name__}: {0}'.format(exc)) from exc
        self._stdout = self._process.stdout
        self._feeder = threading.Thread(
            target=self._feed, args=(byte_iterator,), daemon=True
        )
        self._feeder.start()

    def _feed(self, byte_iterator):
        stdin = self._process.stdin
        try:
            for chunk in byte_iterator:
                if chunk:
                    stdin.write(chunk)
        except (BrokenPipeError, OSError, ValueError):
            # Player stopped / process was cleaned up while we were still writing.
            pass
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    def read(self):
        # BufferedReader.read blocks until FRAME_SIZE bytes are available or EOF.
        ret = self._stdout.read(Encoder.FRAME_SIZE)
        if len(ret) != Encoder.FRAME_SIZE:
            return b''
        return ret

    def is_opus(self):
        return False

    def cleanup(self):
        proc = self._process
        if proc is None:
            return
        proc.kill()
        if proc.poll() is None:
            proc.communicate()
        self._process = None


class FFmpegPCMAudio(discord.AudioSource):
    def __init__(self, source, *, executable='ffmpeg', pipe=False, stderr=None, before_options=None, options=None):
        stdin = None if not pipe else source
        args = [executable]
        if isinstance(before_options, str):
            args.extend(shlex.split(before_options))
        args.append('-i')
        args.append('-' if pipe else source)
        args.extend(('-f', 's16le', '-ar', '48000', '-ac', '2', '-loglevel', 'warning'))
        if isinstance(options, str):
            args.extend(shlex.split(options))
        args.append('pipe:1')
        self._process = None
        try:
            self._process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)
            self._stdout = io.BytesIO(
                self._process.communicate(input=stdin)[0]
            )
        except FileNotFoundError:
            raise discord.ClientException(executable + ' was not found.') from None
        except subprocess.SubprocessError as exc:
            raise discord.ClientException('Popen failed: {0.__class__.__name__}: {0}'.format(exc)) from exc
    def read(self):
        ret = self._stdout.read(Encoder.FRAME_SIZE)
        if len(ret) != Encoder.FRAME_SIZE:
            return b''
        return ret
    def cleanup(self):
        proc = self._process
        if proc is None:
            return
        proc.kill()
        if proc.poll() is None:
            proc.communicate()

        self._process = None