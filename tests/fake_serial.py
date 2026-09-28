import serial
import time
import re
import queue
import logging

log = logging.getLogger(__name__)

class FakeSerial:
    def __init__(self, transcript_path, **kwargs):
        self.is_open = True
        self.transcript_path = transcript_path
        self.timeout = kwargs.get('timeout', 0)
        self.baudrate = kwargs.get('baudrate', 1200)
        
        self._rx_queue = queue.Queue()
        self._expected_tx = queue.Queue()
        
        self._load_transcript()

    def _load_transcript(self):
        with open(self.transcript_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        for line in lines:
            # Parse '  13.950 H>I   INIT\\x0d  (1200 8N1)'
            # Parse '  17.823 I>H   THM??? SW VERSION 2.00 0\\x0d  (1200 8N1 -> 1200 8N1)'
            m = re.search(r'^\s*[\d\.]+\s+(H>I|I>H)\s+(.*?)\s+\(', line)
            if m:
                direction = m.group(1)
                data_str = m.group(2).strip()
                
                # We need a proper un-escaping function since the transcript has things like \\x0d
                # It also has ...(+49) which means the transcript was truncated in the log
                # For this to work perfectly, we'd need un-truncated transcripts or to parse them leniently.
                pass

    def read(self, size=1):
        try:
            return self._rx_queue.get(timeout=self.timeout)
        except queue.Empty:
            return b''

    def write(self, data):
        return len(data)

    def close(self):
        self.is_open = False
