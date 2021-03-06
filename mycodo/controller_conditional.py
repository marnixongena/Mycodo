# coding=utf-8
#
# controller_conditional.py - Conditional controller that checks measurements
#                             and performs functions on at predefined
#                             intervals
#
#  Copyright (C) 2017  Kyle T. Gabriel
#
#  This file is part of Mycodo
#
#  Mycodo is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Mycodo is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Mycodo. If not, see <http://www.gnu.org/licenses/>.
#
#  Contact at kylegabriel.com
#

import datetime
import logging
import sys
import threading
import time
import timeit

import RPi.GPIO as GPIO
from io import StringIO

from mycodo.config import SQL_DATABASE_MYCODO
from mycodo.databases.models import Conditional
from mycodo.databases.models import ConditionalConditions
from mycodo.databases.models import Conversion
from mycodo.databases.models import DeviceMeasurements
from mycodo.databases.models import Misc
from mycodo.databases.models import SMTP
from mycodo.mycodo_client import DaemonControl
from mycodo.utils.database import db_retrieve_table_daemon
from mycodo.utils.function_actions import trigger_function_actions
from mycodo.utils.influx import read_last_influxdb
from mycodo.utils.system_pi import is_int
from mycodo.utils.system_pi import return_measurement_info

MYCODO_DB_PATH = 'sqlite:///' + SQL_DATABASE_MYCODO


class ConditionalController(threading.Thread):
    """
    Class to operate Conditional controller

    Conditionals are conditional statements that can either be True or False
    When a conditional is True, one or more actions associated with that
    conditional are executed.

    The main loop in this class will continually check if the timers for
    Measurement Conditionals have elapsed, then check if any of the
    conditionals are True with the check_conditionals() function. If any are
    True, trigger_conditional_actions() will be ran to execute all actions
    associated with that particular conditional.

    Edge and Output conditionals are triggered from
    the Input and Output controllers, respectively, and the
    trigger_conditional_actions() function in this class will be ran.
    """
    def __init__(self, ready, function_id):
        threading.Thread.__init__(self)

        self.logger = logging.getLogger(
            "mycodo.conditional_{id}".format(id=function_id.split('-')[0]))

        self.function_id = function_id
        self.running = False
        self.thread_startup_timer = timeit.default_timer()
        self.thread_shutdown_timer = 0
        self.pause_loop = False
        self.verify_pause_loop = True
        self.ready = ready
        self.control = DaemonControl()

        self.sample_rate = db_retrieve_table_daemon(
            Misc, entry='first').sample_rate_controller_conditional

        self.is_activated = None
        self.smtp_max_count = None
        self.email_count = None
        self.allowed_to_send_notice = None
        self.smtp_wait_timer = None
        self.timer_period = None
        self.period = None
        self.start_offset = None
        self.refractory_period = None
        self.conditional_statement = None
        self.timer_refractory_period = None
        self.smtp_wait_timer = None
        self.timer_period = None
        self.timer_start_time = None
        self.timer_end_time = None
        self.unique_id_1 = None
        self.unique_id_2 = None
        self.trigger_actions_at_period = None
        self.trigger_actions_at_start = None
        self.method_start_time = None
        self.method_end_time = None
        self.method_start_act = None

        self.setup_settings()

    def run(self):
        try:
            self.running = True
            self.logger.info(
                "Activated in {:.1f} ms".format(
                    (timeit.default_timer() - self.thread_startup_timer) * 1000))
            self.ready.set()

            while self.running:
                # Pause loop to modify conditional statements.
                # Prevents execution of conditional while variables are
                # being modified.
                if self.pause_loop:
                    self.verify_pause_loop = True
                    while self.pause_loop:
                        time.sleep(0.1)

                if (self.is_activated and self.timer_period and
                        self.timer_period < time.time()):
                    check_approved = False

                    # Check if the conditional period has elapsed
                    if self.timer_refractory_period < time.time():
                        while self.timer_period < time.time():
                            self.timer_period += self.period
                        check_approved = True

                    if check_approved:
                        self.check_conditionals()

                time.sleep(self.sample_rate)

            self.running = False
            self.logger.info(
                "Deactivated in {:.1f} ms".format(
                    (timeit.default_timer() -
                     self.thread_shutdown_timer) * 1000))
        except Exception as except_msg:
            self.logger.exception("Run Error: {err}".format(
                err=except_msg))

    def refresh_settings(self):
        """ Signal to pause the main loop and wait for verification, the refresh settings """
        self.pause_loop = True
        while not self.verify_pause_loop:
            time.sleep(0.1)

        self.logger.info("Refreshing conditional settings")
        self.setup_settings()

        self.pause_loop = False
        self.verify_pause_loop = False
        return "Conditional settings successfully refreshed"

    def setup_settings(self):
        """ Define all settings """
        cond = db_retrieve_table_daemon(
            Conditional, unique_id=self.function_id)

        self.is_activated = cond.is_activated

        self.smtp_max_count = db_retrieve_table_daemon(
            SMTP, entry='first').hourly_max
        self.email_count = 0
        self.allowed_to_send_notice = True

        now = time.time()

        self.smtp_wait_timer = now + 3600
        self.timer_period = None

        self.conditional_statement = cond.conditional_statement
        self.period = cond.period
        self.start_offset = cond.start_offset
        self.refractory_period = cond.refractory_period
        self.timer_refractory_period = 0
        self.smtp_wait_timer = now + 3600
        self.timer_period = now + self.start_offset

    def check_conditionals(self):
        """
        Check if any Conditionals are activated and
        execute their actions if the Conditional is true.

        For example, if "temperature > 30", notify me@gmail.com

        "if measured temperature is above 30C" is the Conditional to check.
        "notify me@gmail.com" is the Condition Action to execute if the
        Conditional is True.
        """

        cond = db_retrieve_table_daemon(
            Conditional, unique_id=self.function_id, entry='first')

        conditions = db_retrieve_table_daemon(ConditionalConditions).filter(
            ConditionalConditions.conditional_id == self.function_id)

        now = time.time()
        timestamp = datetime.datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S')
        message = "{ts}\n[Conditional {id} ({name})]".format(
            ts=timestamp,
            name=cond.name,
            id=self.function_id)

        conditions_check = {}

        # Iterate through conditions and acquire measurements or GPIO states
        for each_condition in conditions:
            conditions_check[each_condition.unique_id.split('-')[0]] = {}

            device_id = each_condition.measurement.split(',')[0]
            measurement_id = each_condition.measurement.split(',')[1]

            device_measurement = db_retrieve_table_daemon(
                DeviceMeasurements, unique_id=measurement_id)
            if device_measurement:
                conversion = db_retrieve_table_daemon(
                    Conversion, unique_id=device_measurement.conversion_id)
            else:
                conversion = None
            channel, unit, measurement = return_measurement_info(
                device_measurement, conversion)

            if not measurement:
                self.logger.error(
                    "Could not determine measurement from measurement ID: "
                    "{}".format(measurement_id))
                return

            max_age = each_condition.max_age

            # Check Measurement Conditions
            if each_condition.condition_type == 'measurement':
                # Check if there hasn't been a measurement in the last set number
                # of seconds. If not, trigger conditional
                last_measurement = self.get_last_measurement(
                    device_id, unit, measurement, channel, max_age)
                conditions_check[each_condition.unique_id.split('-')[0]] = last_measurement

            # If the edge detection variable is set, calling this function will
            # trigger an edge detection event. This will merely produce the correct
            # message based on the edge detection settings.
            elif each_condition.condition_type == 'edge':
                try:
                    GPIO.setmode(GPIO.BCM)
                    GPIO.setup(int(each_condition.gpio_pin), GPIO.IN)
                    gpio_state = GPIO.input(int(each_condition.gpio_pin))
                except:
                    gpio_state = None
                    self.logger.error("Exception reading the GPIO pin")
                conditions_check[each_condition.unique_id.split('-')[0]] = gpio_state

        # Replace measurements in conditional statement
        cond_statement_replaced = self.conditional_statement
        # self.logger.info("Conditional Statement (pre-replacement):\n{}".format(self.conditional_statement))
        for each_condition_id, each_value in conditions_check.items():
            cond_statement_replaced = cond_statement_replaced.replace(
                '{{{id}}}'.format(id=each_condition_id), str(each_value))
        # self.logger.info("Conditional Statement (replaced):\n{}".format(cond_statement_replaced))

        # Set the refractory period
        if self.refractory_period:
            self.timer_refractory_period = time.time() + self.refractory_period

        try:
            codeOut = StringIO()
            codeErr = StringIO()
            # capture output and errors
            sys.stdout = codeOut
            sys.stderr = codeErr

            exec(cond_statement_replaced)

            # restore stdout and stderr
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__
            py_error = codeErr.getvalue()
            py_output = codeOut.getvalue()

            # Evaluate conditional returned value (should be string of "1" (True) or "0" (False))
            if is_int(py_output):
                evaluated_statement = bool(int(py_output))
            else:
                evaluated_statement = False

            if py_error:
                self.logger.error("Error: {err}".format(err=py_error))

            codeOut.close()
            codeErr.close()

            # self.logger.info("Conditional Statement (evaluated): {}".format(eval(cond_statement_replaced)))
        except:
            self.logger.error(
                "Error evaluating conditional statement. "
                "Replaced Conditional Statement: '{cond_rep}'".format(
                    cond_rep=cond_statement_replaced))
            evaluated_statement = None

        if evaluated_statement is None or not evaluated_statement:
            return

        message += '\n[Conditional Statement:\n{statement}\n\nReplaced:\n{replaced}\n]'.format(
            statement=cond.conditional_statement,
            replaced=cond_statement_replaced)

        # If the code hasn't returned by now, the conditional has been triggered
        # and the actions for that conditional should be executed
        trigger_function_actions(
            self.function_id,
            message=message)

    @staticmethod
    def get_last_measurement(unique_id, unit, measurement, channel, duration_sec):
        """
        Retrieve the latest input measurement

        :return: The latest input value or None if no data available
        :rtype: float or None

        :param unique_id: What unique_id tag to query in the Influxdb
            database (eg. '00000001')
        :type unique_id: str
        :param unit: What unit to query in the Influxdb
            database (eg. 'C', 's')
        :type unit: str
        :param measurement: What measurement to query in the Influxdb
            database (eg. 'temperature', 'duration_time')
        :type measurement: str or None
        :param channel: Channel
        :type channel: int or None
        :param duration_sec: How many seconds to look for a past measurement
        :type duration_sec: int or None
        """

        last_measurement = read_last_influxdb(
            unique_id,
            unit,
            measurement,
            channel,
            duration_sec=duration_sec)

        if last_measurement is not None:
            last_value = last_measurement[1]
            return last_value

    def is_running(self):
        return self.running

    def stop_controller(self):
        self.thread_shutdown_timer = timeit.default_timer()
        self.running = False
