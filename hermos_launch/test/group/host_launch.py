"""Run the host supervisor in its own launch process for real death/signal tests."""

import os
from pathlib import Path
import sys

from launch import LaunchDescription, LaunchService
from launch.actions import EmitEvent, RegisterEventHandler, TimerAction
from launch.event_handlers import OnProcessIO
from launch.events import Shutdown

from hermos_launch.actions import HermosPeerGroup
from hermos_launch.actions import group as module
from hermos_launch.description import HermosPeer


directory = Path(sys.argv[1])
os.environ['AMENT_PREFIX_PATH'] = str(directory / 'prefix') + ':' + os.environ['AMENT_PREFIX_PATH']
module.supervisor_executable = lambda environment: str(Path(__file__).with_name('host_supervisor.py'))
peer = HermosPeer(peer_id='observer', role='metadata', package='host_peers', executable='probe',
                 registered=True, arguments=[str(directory / 'peer.json'), 'held', sys.argv[2],
                                             '70' if sys.argv[3] == 'shutdown-failure' else '0'])
group = HermosPeerGroup(peers=[peer], log_directory=str(directory), startup_timeout=5,
                       stop_timeout=8, max_restarts=0)
shutdown_sent = False


def registered(event):
    global shutdown_sent
    if sys.argv[3].startswith('shutdown') and not shutdown_sent and b'"registration-ready"' in event.text:
        shutdown_sent = True
        return [TimerAction(period=0.2, actions=[EmitEvent(event=Shutdown(reason='host test'))])]


service = LaunchService()
service.include_launch_description(LaunchDescription([
    RegisterEventHandler(OnProcessIO(on_stdout=registered)), group]))
sys.exit(service.run())
