#!/usr/bin/env python3
"""Start the Zamal Operator Control Center on port 8766."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from zamal_core.control_center import ControlCenterServer

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    server = ControlCenterServer(port=port, telegram_receiver=True)
    print(f'Control Center: http://127.0.0.1:{port}')
    server.serve()
