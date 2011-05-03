#!/usr/bin/env python
# Software License Agreement (BSD License)
#
# Copyright (c) 2010, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
# Revision $Id: __init__.py 11217 2010-09-23 21:08:11Z kwc $

import roslib; roslib.load_manifest('turtlebot_node')

"""
ROS Turtlebot node for ROS built on top of turtlebot_driver.
This driver is based on otl_roomba by OTL (otl-ros-pkg).

turtlebot_driver is based on Damon Kohler's pyrobot.py. 
"""

import os
import sys
import select
import threading
import serial
import termios

from math import sin, cos, radians, pi

from turtlebot_driver import Turtlebot, WHEEL_SEPARATION, MAX_WHEEL_SPEED, DriverError
from turtlebot_node.msg import TurtlebotSensorState, Drive, Turtle
from turtlebot_node.srv import SetTurtlebotMode,SetTurtlebotModeResponse, SetDigitalOutputs, SetDigitalOutputsResponse
from turtlebot_node.diagnostics import TurtlebotDiagnostics
from turtlebot_node.gyro import TurtlebotGyro
from turtlebot_node.sense import TurtlebotSensorHandler, \
     ODOM_POSE_COVARIANCE, ODOM_POSE_COVARIANCE2, ODOM_TWIST_COVARIANCE, ODOM_TWIST_COVARIANCE2
from turtlebot_node.songs import bonus

import rospy

from geometry_msgs.msg import Point, Pose, Pose2D, PoseWithCovariance, \
    Quaternion, Twist, TwistWithCovariance, Vector3
from nav_msgs.msg import Odometry 
from tf.broadcaster import TransformBroadcaster
from sensor_msgs.msg import JointState

class TurtlebotNode(object):

    def __init__(self, default_port='/dev/ttyUSB0', default_update_rate=30.0):
        """
        @param default_port: default tty port to use for establishing
            connection to Turtlebot.  This will be overriden by ~port ROS
            param if available.
        """
        self.default_port = default_port
        self.default_update_rate = default_update_rate
        
        self.lock  = threading.RLock()
        self.robot = None
        self.sensor_handler = None
        self.req_cmd_vel = None
        self.cal_offset = 0
        self.cal_buffer = []

        rospy.init_node('turtlebot')
        self._init_params()
        
        self._diagnostics = TurtlebotDiagnostics()
        if self.has_gyro:    
            self._gyro = TurtlebotGyro()
        else:
            self._gyro = None            
            
    def start(self):
        while not rospy.is_shutdown():
            try:
                self.robot = Turtlebot(self.port)
                break
            except serial.serialutil.SerialException, ex:
                rospy.logerr("Failed to open port %s.  Please make sure the Create cable is plugged into the computer. "%self.port)
                rospy.sleep(3.0)
        
        self.sensor_handler = TurtlebotSensorHandler(self.robot)
        self.robot.safe = True

        if rospy.get_param('~bonus', False):
            bonus(self.robot)

        self.robot.control()
        self._init_pubsub()
        
    def _init_params(self):
        self.port = rospy.get_param('~port', self.default_port)
        rospy.loginfo("serial port: %s"%(self.port))

        self.update_rate = rospy.get_param('~update_rate', self.default_update_rate)
        rospy.loginfo("update_rate: %s"%(self.update_rate))

        self.drive_mode = rospy.get_param('~drive_mode', 'twist')
        rospy.loginfo("drive mode: %s"%(self.drive_mode))

        self.has_gyro = rospy.get_param('~has_gyro', True)
        rospy.loginfo("has gyro: %s"%(self.has_gyro))

        self.odom_angular_scale_correction = rospy.get_param('~odom_angular_scale_correction', 1.0)
        self.odom_linear_scale_correction = rospy.get_param('~odom_linear_scale_correction', 1.0)
        self.cmd_vel_timeout = rospy.Duration(rospy.get_param('~cmd_vel_timeout', 0.6))
        self.stop_motors_on_bump = rospy.get_param('~stop_motors_on_bump', False)
        self.min_abs_yaw_vel = rospy.get_param('~min_abs_yaw_vel', None)
        
    def _init_pubsub(self):
        self.sensor_state_pub = rospy.Publisher('~sensor_state', TurtlebotSensorState)
        self.joint_states_pub = rospy.Publisher('joint_states', JointState)
        self.operating_mode_srv = rospy.Service('~set_operation_mode', SetTurtlebotMode, self.set_operation_mode)
        self.digital_output_srv = rospy.Service('~set_digital_outputs', SetDigitalOutputs, self.set_digital_outputs)

        self.odom_pub = rospy.Publisher('odom', Odometry)

        if self.drive_mode == 'twist':
            self.cmd_vel_sub = rospy.Subscriber('~cmd_vel', Twist, self.cmd_vel)
            self.drive_cmd = self.robot.direct_drive            
        elif self.drive_mode == 'drive':
            self.cmd_vel_sub = rospy.Subscriber('~cmd_vel', Drive, self.cmd_vel)
            self.drive_cmd = self.robot.drive 
        elif self.drive_mode == 'turtle':
            self.cmd_vel_sub = rospy.Subscriber('~cmd_vel', Turtle, self.cmd_vel)
            self.drive_cmd = self.robot.direct_drive
        else:
            rospy.logerr("unknown drive mode :%s"%(self.drive_mode))

    def cmd_vel(self, msg):
        # Clamp to min abs yaw velocity, to avoid trying to rotate at low
        # speeds, which doesn't work well.
        if self.min_abs_yaw_vel is not None and msg.angular.z != 0.0 and abs(msg.angular.z) < self.min_abs_yaw_vel:
            msg.angular.z = self.min_abs_yaw_vel if msg.angular.z > 0.0 else -self.min_abs_yaw_vel
        if self.drive_mode == 'twist':
            # convert twist to direct_drive args
            ts  = msg.linear.x * 1000 # m -> mm
            tw  = msg.angular.z  * (WHEEL_SEPARATION / 2)
            # Prevent saturation at max wheel speed when a compound command is sent.
            if ts > 0:
                ts = min(ts, MAX_WHEEL_SPEED - tw)
            else:
                ts = max(ts, -MAX_WHEEL_SPEED + tw)
            self.req_cmd_vel = int(ts - tw), int(ts + tw)
        elif self.drive_mode == 'turtle':
            # convert to direct_drive args
            ts  = msg.linear * 1000 # m -> mm
            tw  = msg.angular  * (WHEEL_SEPARATION / 2)
            self.req_cmd_vel = int(ts - tw), int(ts + tw)
        elif self.drive_mode == 'drive':
            # convert twist to drive args, m->mm (velocity, radius)
            self.req_cmd_vel = msg.velocity * 1000, msg.radius * 1000

    def set_operation_mode(self,req):
        if req.mode == 1: #passive
            self._robot_run_passive()
        elif req.mode == 2: #safe
            self._robot_run_safe()
        elif req.mode == 3: #full
            self._robot_run_full()
        else:
            rospy.logerr("Requested an invalid mode.")
            return SetTurtlebotModeResponse(False)
        return SetTurtlebotModeResponse(True)

    def _robot_run_passive(self):
        """
        Set robot into passive run mode
        """
        rospy.logdebug("Setting turtlebot to passive mode.")
        #setting all the digital outputs to 0
        self.robot.set_digital_outputs([0, 0, 0])
        self.robot.passive()
        
    def _robot_reboot(self):
        """
        Perform a soft-reset of the Create
        """
        self._set_digital_outputs([0, 0, 0])
        self.robot.soft_reset()

    def _robot_run_safe(self):
        """
        Set robot into safe run mode
        """
        rospy.logdebug("Setting turtlebot to safe mode.")
        self.robot.set_digital_outputs([1, 0, 0])
        self.robot.safe = True
        self.robot.control()

    def _robot_run_full(self):
        """
        Set robot into full run mode
        """
        rospy.logdebug("Setting turtlebot to full mode.")
        self._set_digital_outputs([1, 0, 0])
        self.robot.safe = False
        self.robot.control()
        
    def _set_digital_outputs(self, outputs):
        assert len(outputs) == 3, 'Expecting 3 output states.'
        byte = 0
        for output, state in enumerate(outputs):
            byte += (2 ** output) * int(state) 
        self.robot.set_digital_outputs(byte)
        self.sensor_state.user_digital_outputs = byte
        
    def set_digital_outputs(self,req):
        outputs = [req.digital_out_0,req.digital_out_1, req.digital_out_2]
        self._set_digital_outputs(outputs)
        return SetDigitalOutputsResponse(True)

    def sense(self, sensor_state):
        self.sensor_handler.get_all(sensor_state)

        #check if we're not moving and update the calibration offset
        #to account for any calibration drift due to temperature
        if self.has_gyro and \
               sensor_state.requested_right_velocity == 0 and \
               sensor_state.requested_left_velocity == 0 and \
               sensor_state.distance == 0:
            self._gyro.update_calibration(sensor_state)
            # TODO: move cal_offset, cal_buffer into _gyro object
            # TODO: get rid of has_gyro and just _gyro
            self.cal_offset, self.cal_buffer = self._gyro.compute_cal_offset()

    def spin(self):
        
        # state
        pos2d = Pose2D()
        s = self.sensor_state 
        odom = Odometry(header=rospy.Header(frame_id="odom"), child_frame_id='base_footprint')
        last_cmd_vel = 0, 0
        last_cmd_vel_time = rospy.get_rostime()
        last_diagnostics_time = rospy.get_rostime()
        js = JointState(name = ["left_wheel_joint", "right_wheel_joint", "front_castor_joint", "back_castor_joint"],
                        position=[0,0,0,0], velocity=[0,0,0,0], effort=[0,0,0,0])

        r = rospy.Rate(self.update_rate)
        while not rospy.is_shutdown():
            try:
                last_time = s.header.stamp

                # SENSE/COMPUTE STATE
                try:
                    self.sense(s)
                    transform = self.compute_odom(s, pos2d, last_time, odom)
                except select.error:
                    # packet read can get interrupted, restart loop to
                    # check for exit conditions
                    continue
                js.header.stamp = rospy.get_rostime()

                if s.charging_sources_available > 0 and \
                       s.oi_mode == 1 \
                       and (s.charge < 0.93*s.capacity):
                    self._robot_reboot()

                # PUBLISH STATE
                self.sensor_state_pub.publish(s)
                self.odom_pub.publish(odom)
                self.joint_states_pub.publish(js)
                if (s.header.stamp-last_diagnostics_time).to_sec() > 1.0:
                    last_diagnostics_time = s.header.stamp
                    # TODO: pass in self._gyro instead
                    self._diagnostics.publish_diagnostics(s, self._gyro)
                if self.has_gyro and self.cal_offset !=0:  
                    self._gyro.publish_imu_data(s, last_time)

                # ACT
                if self.req_cmd_vel is not None:
                    # check for velocity command and set the robot into full mode
                    if s.oi_mode < 3 and s.charging_sources_available != 1:
                        self._robot_run()

                    # check for bumper contact and limit drive command
                    req_cmd_vel = self.check_bumpers(s, self.req_cmd_vel)

                    # Set to None so we know it's a new command
                    self.req_cmd_vel = None 
                    # reset time for timeout
                    last_cmd_vel_time = last_time

                else:
                    #zero commands on timeout
                    if last_time - last_cmd_vel_time > self.cmd_vel_timeout:
                        last_cmd_vel = 0,0
                    # double check bumpers
                    req_cmd_vel = self.check_bumpers(s, last_cmd_vel)

                # send command
                self.drive_cmd(*req_cmd_vel)
                # record command
                last_cmd_vel = req_cmd_vel

                r.sleep()

            except DriverError, ex:
                rospy.logerr("Failed to contact device with error: [%s]. Please check that the Create is powered on and that the connector is plugged into the Create."%ex)
                rospy.sleep(3.0)
            except termios.error, ex:
                rospy.logfatal("Write to port %s failed.  Did the usb cable become unplugged?"%self.port)
                break

    def check_bumpers(self, s, cmd_vel):
        # Safety: disallow forward motion if bumpers or wheeldrops 
        # are activated.
        # TODO: check bumps_wheeldrops flags more thoroughly, and disable
        # all motion (not just forward motion) when wheeldrops are activated
        forward = (cmd_vel[0] + cmd_vel[1]) > 0
        if self.stop_motors_on_bump and s.bumps_wheeldrops > 0 and forward:
            return (0,0)
        else:
            return cmd_vel

    def compute_odom(self, sensor_state, pos2d, last_time, odom):
        """
        Compute current odometry.  Updates odom instance and returns tf
        transform. compute_odom() does not set frame ids or covariances in
        Odometry instance.  It will only set stamp, pose, and twist.

        @param sensor_state: Current sensor reading
        @type  sensor_state: TurtlebotSensorState
        @param pos2d: Current position
        @type  pos2d: geometry_msgs.msg.Pose2D
        @param last_time: time of last sensor reading
        @type  last_time: rospy.Time
        @param odom: Odometry instance to update. 
        @type  odom: nav_msgs.msg.Odometry

        @return: transform
        @rtype: ( (float, float, float), (float, float, float, float) )
        """
        # based on otl_roomba by OTL <t.ogura@gmail.com>

        current_time = sensor_state.header.stamp
        dt = (current_time - last_time).to_sec()

        # this is really delta_distance, delta_angle
        d  = sensor_state.distance * self.odom_linear_scale_correction #correction factor from calibration
        angle = sensor_state.angle * self.odom_angular_scale_correction #correction factor from calibration

        x = cos(angle) * d
        y = -sin(angle) * d

        last_angle = pos2d.theta
        pos2d.x += cos(last_angle)*x - sin(last_angle)*y
        pos2d.y += sin(last_angle)*x + cos(last_angle)*y
        pos2d.theta += angle

        # Turtlebot quaternion from yaw. simplified version of tf.transformations.quaternion_about_axis
        odom_quat = (0., 0., sin(pos2d.theta/2.), cos(pos2d.theta/2.))
        #rospy.logerr("theta: %f odom_quat %s"%(pos2d.theta, str(odom_quat)))

        # construct the transform
        transform = (pos2d.x, pos2d.y, 0.), odom_quat

        # update the odometry state
        odom.header.stamp = current_time
        odom.pose.pose   = Pose(Point(pos2d.x, pos2d.y, 0.), Quaternion(*odom_quat))
        odom.twist.twist = Twist(Vector3(d/dt, 0, 0), Vector3(0, 0, angle/dt))
        if sensor_state.requested_right_velocity == 0 and \
               sensor_state.requested_left_velocity == 0 and \
               sensor_state.distance == 0:
            odom.pose.covariance = ODOM_POSE_COVARIANCE2
            odom.twist.covariance = ODOM_TWIST_COVARIANCE2
        else:
            odom.pose.covariance = ODOM_POSE_COVARIANCE
            odom.twist.covariance = ODOM_TWIST_COVARIANCE

        # return the transform
        return transform
                
def turtlebot_main():
    c = TurtlebotNode()
    c.start()
    c.spin()

if __name__ == '__main__':
    turtlebot_main()
