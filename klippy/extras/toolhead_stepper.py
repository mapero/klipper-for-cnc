# Code for coordinating events on the printer toolhead
#
# Copyright (C) 2016-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, logging, importlib
import mcu, chelper, kinematics.extruder
import time

# Move, MoveQueue, LOOKAHEAD_FLUSH_TIME, 
# MIN_KIN_TIME, MOVE_BATCH_TIME, SDS_CHECK_TIME,
# DRIP_SEGMENT_TIME, DRIP_TIME, 
# DripModeEndSignal
from toolhead import *

# Common suffixes: _d is distance (in mm), _v is velocity (in
#   mm/second), _v2 is velocity squared (mm^2/s^2), _t is time (in
#   seconds), _r is ratio (scalar between 0.0 and 1.0)

""" Notes on 'print_time': https://www.klipper3d.org/Code_Overview.html#time
The print time is synchronized to the main micro-controller clock (the micro-controller defined in the "[mcu]" config section). 

It is a floating point number stored as seconds and is relative to when the main mcu was last restarted. 

It is possible to convert from a "print time" to the main micro-controller's hardware clock by multiplying the print time by 
the mcu's statically configured frequency rate. 

The high-level host code uses print times to calculate almost all physical actions (eg, head movement, heater changes, etc.). 

Within the host code, print times are generally stored in variables named print_time or move_time.
"""

# Main code to track events (and their timing) on the printer toolhead
class ToolHeadStepper:
    """Main toolhead class.

    Example config:
    
    [printer]
    kinematics: cartesian
    axis: XYZ  # Optional: XYZ or XYZABC
    kinematics_abc: cartesian_abc # Optional
    max_velocity: 5000
    max_z_velocity: 250
    max_accel: 1000
    
    TODO:
      - The "checks" still have the XYZ logic.
      - Homing is not implemented for ABC.
    """
    def __init__(self, config):
        # NOTE: amount of non-extruder axes: XYZ=3, XYZABC=6.
        self.axis_names = config.get('axis', 'XYZ')  # "XYZ" / "XYZABC"
        self.axis_count = len(self.axis_names)
        
        # TODO: support more kinematics.
        self.supported_kinematics = ["cartesian", "cartesian_abc", "none"]
        
        logging.info(f"\n\nToolHead: starting setup with axes: {self.axis_names}.\n\n")
        
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.all_mcus = [m for n, m in self.printer.lookup_objects(module='mcu')]
        self.mcu = self.all_mcus[0]
        self.can_pause = True
        if self.mcu.is_fileoutput():
            # NOTE: This triggers if 'debugoutput' is not None in the config,
            #       see "mcu.py".
            self.can_pause = False
        self.move_queue = MoveQueue(self)
        self.commanded_pos = [0.0 for i in range(self.axis_count + 1)]
        self.printer.register_event_handler("klippy:shutdown",
                                            self._handle_shutdown)
        
        # Prefix for event names
        self.event_prefix = "toolhead_stepper:" # NOTE: originally "toolhead:"
        
        # Velocity and acceleration control
        # NOTE: from the "[printer]" config section.
        self.max_velocity = config.getfloat('max_velocity', above=0.)
        self.max_accel = config.getfloat('max_accel', above=0.)
        self.requested_accel_to_decel = config.getfloat(
            'max_accel_to_decel', self.max_accel * 0.5, above=0.)
        self.max_accel_to_decel = self.requested_accel_to_decel
        self.square_corner_velocity = config.getfloat(
            'square_corner_velocity', 5., minval=0.)
        self.junction_deviation = 0.
        self._calc_junction_deviation()
        
        # Print time tracking
        # NOTE: Uncertain config section, parameters not documented.
        self.buffer_time_low = config.getfloat(
            'buffer_time_low', 1.000, above=0.)
        self.buffer_time_high = config.getfloat(
            'buffer_time_high', 2.000, above=self.buffer_time_low)
        self.buffer_time_start = config.getfloat(
            'buffer_time_start', 0.250, above=0.)
        self.move_flush_time = config.getfloat(
            'move_flush_time', 0.050, above=0.)
        self.print_time = 0.
        self.special_queuing_state = "Flushed"
        self.need_check_stall = -1.
        self.flush_timer = self.reactor.register_timer(self._flush_handler)
        self.move_queue.set_flush_time(self.buffer_time_high)
        self.idle_flush_print_time = 0.
        self.print_stall = 0
        self.drip_completion = None
        
        # Kinematic step generation scan window time tracking
        self.kin_flush_delay = SDS_CHECK_TIME
        self.kin_flush_times = []
        self.force_flush_time = self.last_kin_move_time = 0.
        
        # Setup iterative solver methods
        ffi_main, ffi_lib = chelper.get_ffi()
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.step_generators = []
        
        # NOTE: load the gcode objects (?)
        gcode = self.printer.lookup_object('gcode')
        self.Coord = gcode.Coord
        
        # NOTE: Load trapq (iterative solvers) and kinematics for the requested axes.
        self.kinematics = {}
        self.load_axes(config=config)
        
        # Create extruder kinematics class
        # NOTE: setup a dummy extruder at first, replaced later if configured.
        self.extruder = kinematics.extruder.DummyExtruder(self.printer)
        
        # Register commands
        gcode.register_mux_command('G4', 'TOOLHEAD', self.cmd_G4)
        gcode.register_mux_command('M400', 'TOOLHEAD', self.cmd_M400)
        gcode.register_mux_command('SET_VELOCITY_LIMIT', 'TOOLHEAD',
                               self.cmd_SET_VELOCITY_LIMIT,
                               desc=self.cmd_SET_VELOCITY_LIMIT_help)
        gcode.register_mux_command('M204', 'TOOLHEAD', self.cmd_M204)
    
    # Load axes abstraction
    def load_axes(self, config):
        """Convnenience function to setup kinematics and trapq objects for the toolhead.

        The definition of this function contains several "hardcoded" variables that should
        be moved to a separate config file eventually, or be otherwise configurable.

        Args:
            config (_type_): Klipper configuration object.
        """
        
        # Setup XYZ axes
        if "XYZ" in self.axis_names:
            # Create XYZ kinematics class, and its XYZ trapq (iterative solver).
            self.kin, self.trapq = self.load_kinematics(config=config, 
                                                        config_name='kinematics',
                                                        axes_ids = [0, 1, 2])
            # Save the kinematics to the dict.
            self.kinematics["XYZ"] = self.kin
        else:
            self.kin, self.trapq = None, None
        
        # Setup ABC axes
        if "ABC" in self.axis_names:
            # Create ABC kinematics class, and its ABC trapq (iterative solver).
            self.kin_abc, self.abc_trapq = self.load_kinematics(config=config, 
                                                                config_name='kinematics_abc',
                                                                axes_ids=[3, 4 ,5])
            # Save the kinematics to the dict.
            self.kinematics["ABC"] = self.kin_abc
        else:
            self.kin_abc, self.abc_trapq = None, None
    
    # Load kinematics object
    def load_kinematics(self, config, axes_ids, config_name='kinematics'):
        """Load kinematics for a set of axes.

        Note: this requires the Kinematics module to accept a "trapq" object,
        which it must use. Thus, the "load_kinematics" function must also
        be able to pass the object to the instantiation of the new knimeatics class.

        Most kinematics in this branch have not been updated.

        Args:
            config (_type_): Klipper configuration object.
            axes_ids (list): List of integers spevifying which of the "toolhead position" elements correspond to the axes of the new kinematic.
            config_name (str, optional): Name of the kinematics in the config. Defaults to 'kinematics'.

        Returns:
            CartKinematics: Kinematics object.
        """
        # NOTE: get the "kinematics" type from "[printer]".
        kin_name = config.get(config_name)

        # TODO: Support other kinematics is due. Error out for now.
        if kin_name not in self.supported_kinematics:
            msg = f"Error loading kinematics '{kin_name}'. Currently supported kinematics: {self.supported_kinematics}"
            logging.exception(msg)
            raise config.error(msg)
        
        # Create a Trapq for the kinematics
        ffi_main, ffi_lib = chelper.get_ffi()
        trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)  # TrapQ()
        
        # Set up the kinematics object
        try:
            # Import the python module file for the requested kinematic.
            mod = importlib.import_module('kinematics.' + kin_name)
            # Run the modules setup function.
            kin = mod.load_kinematics(self, config, trapq)
            # Specify which of the toolhead position elements correspon to the axis.
            kin.axis = axes_ids.copy()
        except config.error as e:
            raise
        except self.printer.lookup_object('pins').error as e:
            raise
        except:
            msg = "Error loading kinematics '%s'" % (kin_name,)
            logging.exception(msg)
            raise config.error(msg)
        
        return kin, trapq
    
    # Print time tracking
    def _update_move_time(self, next_print_time):
        batch_time = MOVE_BATCH_TIME
        # NOTE: called by "flush_step_generation", "_process_moves", 
        #       "dwell", and "_update_drip_move_time".
        # NOTE: This function updates "self.print_time" directly.
        #       It updates "self.print_time" until it is greater than
        #       the provided "next_print_time".
        # NOTE: It also calls "trapq_finalize_moves" on the extruder and toolhead.
        # NOTE: a possible "use case" in the code is to:
        #           "Generate steps for moves"
        
        logging.info(f"\n\nToolHead: _update_move_time triggered with next_print_time={next_print_time}\n\n")

        kin_flush_delay = self.kin_flush_delay
        # TODO: what is "fft"? It used to be named "last_kin_flush_time".
        fft = self.force_flush_time
        # TODO: I don't yet understand what the loop is meant to accomplish.
        while 1:
            # NOTE: Start by incrementing "print_time" by "batch_time", unless
            #       "next_print_time" is smaller.
            self.print_time = min(self.print_time + batch_time, next_print_time)
            
            # NOTE: Generate steps before the "sg_flush_time" time. That time is defined as
            #       "self.force_flush_time", unless it is after "print_time-kin_flush_delay".
            sg_flush_time = max(fft, self.print_time - kin_flush_delay)
            for sg in self.step_generators:
                # NOTE: "self.step_generators" has been populated with "generate_steps" functions,
                #       one per stepper, by each kinematic class (including the extruder class).
                #       Those functions in turn end up calling "ffi_lib.itersolve_generate_steps"
                #       which are meant to "Generate step times for a range of moves on the trapq".
                sg(sg_flush_time)
                
            # NOTE: Expire moves in the trapq before the "free_time" time.
            #       This is defined as "self.force_flush_time", unless it is
            #       less than "print_time-kin_flush_delay*2" (equivalent in the
            #       case that "fft < self.print_time - kin_flush_delay" avobe).
            free_time = max(fft, sg_flush_time - kin_flush_delay)
            # NOTE: Update move times on the toolhead's trapqs, meaning:
            #       "Expire any moves older than `free_time` from
            #       the trapezoid velocity queue" (see trapq.c).
            for axes in list(self.kinematics):
                # Iterate over ["XYZ", "ABC"].
                kin = self.kinematics[axes]
                logging.info(f"\n\nToolHead._update_move_time calling trapq_finalize_moves on axes={axes} with free_time={free_time}\n\n")
                self.trapq_finalize_moves(kin.trapq, free_time)
            
            # NOTE: "free_time" is smaller than "sg_flush_time" by "kin_flush_delay",
            #       which is defined from "SDS_CHECK_TIME".
            
            # TODO: remove old (pre 6-axis) stuff.
            # # NOTE: Update move times on the toolhead, meaning:
            # #           "Expire any moves older than `free_time` from
            # #           the trapezoid velocity queue" (see trapq.c).
            # self.trapq_finalize_moves(self.trapq, free_time)
            # # NOTE: Setup "self.trapq_finalize_moves" on the ABC trapq as well.
            # self.trapq_finalize_moves(self.abc_trapq, free_time)
            
            # NOTE: Update move times on the extruder by calling
            #       "trapq_finalize_moves" in PrinterExtruder.
            self.extruder.update_move_time(free_time)

            mcu_flush_time = max(fft, sg_flush_time - self.move_flush_time)
            for m in self.all_mcus:
                # NOTE: The following may find and transmit any scheduled steps 
                #       prior to the given 'mcu_flush_time' (see stepcompress.c
                #       and "flush_moves" in mcu.py).
                m.flush_moves(mcu_flush_time)
            if self.print_time >= next_print_time:
                break
    
    def _calc_print_time(self):
        # NOTE: Called during "special" queuing states, 
        #       by "get_last_move_time" or "_process_moves".
        # NOTE: This function updates "self.print_time" directly.
        # NOTE: Also sends a "toolhead:sync_print_time" event, handled by
        #       "handle_sync_print_time" at "idle_timeout.py". It calls
        #       "reactor.update_timer" and sends an "idle_timeout:printing" 
        #       event (which is only handled by tmc2660.py).

        # NOTE: Get the current (host) system time.
        curtime = self.reactor.monotonic()
        
        # NOTE: Method from MCU (at mcu.py) that calls the
        #       "self._clocksync.estimated_print_time" 
        #       method from the ClockSync class (at clocksync.py).
        #       The method uses "get_clock" to get "self.clock_est" 
        #       from the ClockSync class. That object is updated in 
        #       the background by "_handle_clock" which:
        #       "is invoked from background thread" for "MCU clock querying".
        est_print_time = self.mcu.estimated_print_time(curtime)

        # NOTE: Guessing that the following adds potential delays to 
        #       the MCU time, estimating a "minimum print time".
        kin_time = max(est_print_time + MIN_KIN_TIME, self.force_flush_time)
        kin_time += self.kin_flush_delay
        min_print_time = max(est_print_time + self.buffer_time_start, kin_time)

        if min_print_time > self.print_time:
            self.print_time = min_print_time
            self.printer.send_event(self.event_prefix + "sync_print_time",  # "toolhead:sync_print_time"
                                    curtime, est_print_time, self.print_time)
    def _process_moves(self, moves):
        """
        When ToolHead._process_moves() is called, everything about the move is known - its start location, 
        its end location, its acceleration, its start/cruising/end velocity, and distance traveled during 
        acceleration/cruising/deceleration. 
        All the information is stored in the Move() class and is in cartesian space in units of millimeters and seconds.
        
        Klipper uses an iterative solver to generate the step times for each stepper. For efficiency reasons,
        the stepper pulse times are generated in C code. The moves are first placed on a "trapezoid motion queue": 
            ToolHead._process_moves() -> trapq_append() (in klippy/chelper/trapq.c).
            
        Note that the extruder is handled in its own kinematic class:
            ToolHead._process_moves() -> PrinterExtruder.move()
        Since the Move() class specifies the exact movement time and since step pulses are sent to the micro-controller 
        with specific timing, stepper movements produced by the extruder class will be in sync with head movement even
        though the code is kept separate.
        
        The step times are then generated: 
            ToolHead._process_moves() -> ToolHead._update_move_time() -> MCU_Stepper.generate_steps() -> 
            itersolve_generate_steps() -> itersolve_gen_steps_range() (in klippy/chelper/itersolve.c). 
        
        The goal of the iterative solver is to find step times given a function that calculates a stepper 
        position from a time. This is done by repeatedly "guessing" various times until the stepper position 
        formula returns the desired position of the next step on the stepper. The feedback produced from each 
        guess is used to improve future guesses so that the process rapidly converges to the desired time. 
        
        The kinematic stepper position formulas are located in the klippy/chelper/ directory (eg, kin_cart.c, 
        kin_corexy.c, kin_delta.c, kin_extruder.c).
        
        After the iterative solver calculates the step times they are added to an array:
            itersolve_gen_steps_range() -> stepcompress_append() (in klippy/chelper/stepcompress.c).
        
        The next major step is to compress the steps: 
            stepcompress_flush() -> compress_bisect_add() (in klippy/chelper/stepcompress.c)

        Args:
            moves (_type_): _description_
        """
        # NOTE: this ToolHead method is called during the execution of 
        #       the "flush" method in a "MoveQueue" class instance.
        #       The "moves" argument receives a "queue" of moves "ready to be flushed".
        
        # NOTE: logging for tracing activity
        logging.info("\n\nToolHead _process_moves: function triggered.\n\n")
        
        # Resync print_time if necessary
        if self.special_queuing_state:
            if self.special_queuing_state != "Drip":
                # Transition from "Flushed"/"Priming" state to main state
                self.special_queuing_state = ""
                self.need_check_stall = -1.
                # NOTE: updates the "self._next_timer" object in the "reactor".
                self.reactor.update_timer(self.flush_timer, self.reactor.NOW)
            
            # NOTE Update "self.print_time".
            self._calc_print_time()
            # NOTE: Also sends a "toolhead:sync_print_time" event.
            logging.info(f"\n\nToolHead _process_moves: self.print_time={str(self.print_time)}\n\n")
        
        # Queue moves into trapezoid motion queue (trapq)
        # NOTE: the "trapq" is possibly something like a CFFI object.
        #       From the following I interpret that it is actually this
        #       object the one responsible for sending commands to
        #       the MCUs.
        next_move_time = self.print_time
        for move in moves:
            logging.info(f"ToolHead _process_moves: next_move_time={str(next_move_time)}")
            
            for axes in list(self.kinematics):
                # Iterate over["XYZ", "ABC"]
                logging.info("\n\n" + f"toolhead._process_moves: appending move to {axes} trapq.\n\n")
                kin = self.kinematics[axes]
                # NOTE: The moves are first placed on a "trapezoid motion queue" with trapq_append.
                if move.is_kinematic_move:
                    self.trapq_append(
                        kin.trapq, next_move_time,
                        move.accel_t, move.cruise_t, move.decel_t,
                        # NOTE: "kin.axis" is used to select the position value that corresponds
                        #       to the current kinematic axis (e.g. kin.axis is [0,1,2] for the XYZ axis,
                        #       or [3,4,5] for the ABC axis).
                        move.start_pos[kin.axis[0]], move.start_pos[kin.axis[1]], move.start_pos[kin.axis[2]],
                        move.axes_r[kin.axis[0]], move.axes_r[kin.axis[1]], move.axes_r[kin.axis[2]],
                        move.start_v, move.cruise_v, move.accel)
            
            # NOTE: Repeat for the extruder's trapq.
            if move.axes_d[self.axis_count]:
                # NOTE: The extruder stepper move is likely synced to the main
                #       XYZ movement here, by sharing the "next_move_time"
                #       parameter in the call.
                self.extruder.move(print_time=next_move_time, move=move)
            
            # NOTE: The start MCU time for the next move in 
            #       the move queue is calculated here.
            next_move_time = (next_move_time + move.accel_t
                              + move.cruise_t + move.decel_t)
            
            # NOTE: Execute any "callbacks" registered 
            #       to be run at the end of this move.
            for cb in move.timing_callbacks:
                cb(next_move_time)
        
        # Generate steps for moves
        if self.special_queuing_state:
            # NOTE: this block is executed when "special_queuing_state" is not None.
            # NOTE: loging "next_move_time" for tracing.
            logging.info("\n\nToolHead _process_moves: calling _update_drip_move_time with " +
                         f"next_move_time={str(next_move_time)}\n\n")
            # NOTE: this function loops "while self.print_time < next_print_time".
            #       It "pauses before sending more steps" using "drip_completion.wait",
            #       and calls "_update_move_time". 
            self._update_drip_move_time(next_move_time)
        
        # NOTE: "next_move_time" is the last "self.print_time" plus the
        #       time added by the new moves sento to trapq.
        #       Here, it is passed to "_update_move_time" (which updates
        #       "self.print_time" and calls "trapq_finalize_moves") and
        #       to overwrite "self.last_kin_move_time".
        logging.info(f"\n\nToolHead _process_moves: _update_move_time with next_move_time={next_move_time}\n\n")
        self._update_move_time(next_move_time)
        self.last_kin_move_time = max(self.last_kin_move_time, next_move_time)
        logging.info(f"\n\nToolHead _process_moves: last_kin_move_time set to next_move_time={self.last_kin_move_time}\n\n")
        
    def flush_step_generation(self):
        # Transition from "Flushed"/"Priming"/main state to "Flushed" state
        # NOTE: a "use case" for drip moves is to: 'Exit "Drip" state'

        # NOTE: this is the "flush" method from a "MoveQueue" object.
        #       It calls "_process_moves" on the moves in the queue that
        #       are "ready to be flushed", and removes them from the queue.
        self.move_queue.flush()

        # NOTE: the state is set to "Flushed" which is still a
        #       "special" state (i.e. not the "" main state)
        self.special_queuing_state = "Flushed"
        self.need_check_stall = -1.

        # NOTE: updates the "self._next_timer" object in the "reactor",
        #       and sets "flush_timer.waketime" to "self.reactor.NEVER".
        self.reactor.update_timer(self.flush_timer, self.reactor.NEVER)

        # NOTE: sets "self.junction_flush" to "self.buffer_time_high"
        #       in the MoveQueue class. Note that the "junction_flush"
        #       is reset when the "flush" method is called. Not sure
        #       what this accomplishes.
        self.move_queue.set_flush_time(self.buffer_time_high)

        self.idle_flush_print_time = 0.
        
        # Determine actual last "itersolve" flush time
        lastf = self.print_time - self.kin_flush_delay
        
        # Calculate flush time that includes kinematic scan windows
        flush_time = max(lastf, self.last_kin_move_time + self.kin_flush_delay)
        if flush_time > self.print_time:
            # Flush in small time chunks
            # NOTE: the following updates "self.print_time" and
            #       calls "trapq_finalize_moves".
            self._update_move_time(flush_time)
        
        # Flush kinematic scan windows and step buffers
        # NOTE: This is the one place where "self.force_flush_time" can 
        #       be incremented, up to "flush_time".
        self.force_flush_time = max(self.force_flush_time, flush_time)
        self._update_move_time(next_print_time=max(self.print_time,
                                                   self.force_flush_time))
    
    def _flush_lookahead(self):
        if self.special_queuing_state:
            return self.flush_step_generation()
        self.move_queue.flush()
    
    def get_last_move_time(self):
        # NOTE: this method probably returns a "safe" time
        #       which can be used to schedule a new move,
        #       after others have finished.

        # NOTE: The "_flush_lookahead" method calls either:
        #       - flush_step_generation: which updates "self.print_time" through "_update_move_time".
        #       - move_queue.flush: also ends up updating "self.print_time".
        self._flush_lookahead()

        if self.special_queuing_state:
            # NOTE: the "_calc_print_time" function also updates "self.print_time"
            self._calc_print_time()
        
        return self.print_time
    
    def _check_stall(self):
        eventtime = self.reactor.monotonic()
        if self.special_queuing_state:
            if self.idle_flush_print_time:
                # Was in "Flushed" state and got there from idle input
                est_print_time = self.mcu.estimated_print_time(eventtime)
                if est_print_time < self.idle_flush_print_time:
                    self.print_stall += 1
                self.idle_flush_print_time = 0.
            # Transition from "Flushed"/"Priming" state to "Priming" state
            self.special_queuing_state = "Priming"
            self.need_check_stall = -1.
            self.reactor.update_timer(self.flush_timer, eventtime + 0.100)
        # Check if there are lots of queued moves and stall if so
        while 1:
            est_print_time = self.mcu.estimated_print_time(eventtime)
            buffer_time = self.print_time - est_print_time
            stall_time = buffer_time - self.buffer_time_high
            if stall_time <= 0.:
                break
            if not self.can_pause:
                self.need_check_stall = self.reactor.NEVER
                return
            eventtime = self.reactor.pause(eventtime + min(1., stall_time))
        if not self.special_queuing_state:
            # In main state - defer stall checking until needed
            self.need_check_stall = (est_print_time + self.buffer_time_high
                                     + 0.100)
    
    def _flush_handler(self, eventtime):
        """Callback function for the 'self.flush_timer' reactor timer"""
        try:
            print_time = self.print_time
            buffer_time = print_time - self.mcu.estimated_print_time(eventtime)
            if buffer_time > self.buffer_time_low:
                # Running normally - reschedule check
                return eventtime + buffer_time - self.buffer_time_low
            # Under ran low buffer mark - flush lookahead queue
            self.flush_step_generation()
            if print_time != self.print_time:
                self.idle_flush_print_time = self.print_time
        except:
            logging.exception("Exception in flush_handler")
            self.printer.invoke_shutdown("Exception in flush_handler")
        return self.reactor.NEVER
    
    # Movement commands
    def get_position(self):
        return list(self.commanded_pos)
    
    def axes_to_xyz(self, axes):
        """Convert ABC axis IDs to XYZ IDs (i.e. 3,4,5 to 0,1,2).
        
        Has no effect on XYZ IDs
        """
        logging.info(f"\n\ntoolhead.axes_to_xyz: input={axes}\n\n")
        
        xyz_ids = [0, 1, 2, 0, 1, 2]
        
        try:
            if isinstance(axes, list) or isinstance(axes, tuple):
                result = [xyz_ids[i] for i in axes]
            else:
                result = xyz_ids[axes]
        except:
            raise Exception(f"\n\ntoolhead.axes_to_xyz: error with input={axes}\n\n")
        
        logging.info(f"\n\ntoolhead.axes_to_xyz: output={result}\n\n")
        
        return result
    
    def get_elements(self, toolhead_pos, axes):
        return [toolhead_pos[axis] for axis in axes]
    
    def set_position(self, newpos, homing_axes=()):
        logging.info("\n\n" + f"toolhead.set_position: setting newpos={newpos} and homing_axes={homing_axes}\n\n")
        self.flush_step_generation()
            
        # NOTE: Set the position of the axes "trapq".
        for axes in list(self.kinematics):
            # Iterate over["XYZ", "ABC"]
            logging.info("\n\n" + f"toolhead.set_position: setting {axes} trapq position.\n\n")
            kin = self.kinematics[axes]
            # Filter the axis IDs according to the current kinematic
            new_kin_pos = self.get_elements(newpos, kin.axis)
            logging.info("\n\n" + f"toolhead.set_position: using newpos={new_kin_pos}\n\n")
            self.set_kin_trap_position(kin.trapq, new_kin_pos)
        
        # NOTE: Also set the position of the extruder's "trapq".
        #       Runs "trapq_set_position" and "rail.set_position".
        logging.info("\n\n" + f"toolhead.set_position: setting E trapq pos.\n\n")
        self.set_position_e(newpos_e=newpos[self.axis_count], homing_axes=homing_axes)
        
        # NOTE: Set the position of the axes "kinematics".
        for axes in list(self.kinematics):
            # Iterate over["XYZ", "ABC"]
            logging.info("\n\n" + f"toolhead.set_position: setting {axes} kinematic position.\n\n")
            kin = self.kinematics[axes]
            # Filter the axis IDs according to the current kinematic, and convert them to the "0,1,2" range.
            kin_homing_axes = self.axes_to_xyz([axis for axis in homing_axes if axis in kin.axis])
            new_kin_pos = self.get_elements(newpos, kin.axis)
            logging.info("\n\n" + f"toolhead.set_position: using newpos={new_kin_pos} and kin_homing_axes={kin_homing_axes}\n\n")
            self.set_kinematics_position(kin=kin, newpos=new_kin_pos, homing_axes=tuple(kin_homing_axes))
            
        # NOTE: "set_position_e" was inserted above and not after 
        #       updating "commanded_pos" under the suspicion that 
        #       an unmodified "commanded_pos" might be important.
        self.commanded_pos[:] = newpos
        
        # NOTE: this event is mainly recived by gcode_move.reset_last_position,
        #       which updates its "self.last_position" with (presumably) the
        #       "self.commanded_pos" above.
        self.printer.send_event(self.event_prefix + "set_position")  # "toolhead:set_position"
        
    def set_kin_trap_position(self, trapq, newpos):
        """Abstraction of trapq_set_position for different sets of kinematics.

        Args:
            trapq (trapq): trapezoidal queue.
            newpos (list): 3-element list with the new positions for the trapq.
        """
        
        if trapq is not None:
            # NOTE: Set the position of the toolhead's "trapq".
            logging.info("\n\n" + f"toolhead.set_kin_trap_position: setting trapq pos to newpos={newpos}\n\n")
            ffi_main, ffi_lib = chelper.get_ffi()
            ffi_lib.trapq_set_position(self.trapq, self.print_time,
                                    newpos[0], newpos[1], newpos[2])
        else:
            logging.info("\n\n" + f"toolhead.set_kin_trap_position: trapq was None, skipped setting to newpos={newpos}\n\n")
    
    def set_kinematics_position(self, kin, newpos, homing_axes):
        """Abstraction of kin.set_position for different sets of kinematics.

        Args:
            kin (kinematics): Instance of a (cartesian) kinematics class.
            newpos (list): 3-element list with the new positions for the kinematics.
            homing_axes (tuple): 3-element tuple indicating the axes that should have their limits re-applied.
        """
        # NOTE: The "homing_axes" argument is a tuple similar to
        #       "(0,1,2)" (see SET_KINEMATIC_POSITION at "force_move.py"),
        #       used to set axis limits by the (cartesian) kinematics.
        # NOTE: Calls "rail.set_position" on each stepper which in turn
        #       calls "itersolve_set_position" from "itersolve.c".
        # NOTE: Passing only the first three elements (XYZ) to this set_position.
        if kin is not None:
            logging.info("\n\n" + f"toolhead.set_kinematics_position: setting kinematic position with newpos={newpos} and homing_axes={homing_axes}\n\n")
            kin.set_position(newpos, homing_axes=tuple(homing_axes))
        else:
            logging.info("\n\n" + f"toolhead.set_kinematics_position: kin was None, skipped setting to newpos={newpos} and homing_axes={homing_axes}\n\n")

    def set_position_e(self, newpos_e, homing_axes=()):
        """Extruder version of set_position."""
        logging.info("\n\n" + f"toolhead.set_position_e: setting E to newpos={newpos_e}.\n\n")
        
        # Get the active extruder
        extruder = self.get_extruder()  # PrinterExtruder
        
        if extruder.get_name() is None:
            # Do nothing if the extruder is a "Dummy" extruder.
            pass
        else:
            # NOTE: Let the "extruder kinematic" set its position. This will call
            #       set position on the "trapq" and "rail" objects of the
            #       active ExtruderStepper class
            # TODO: the "homing_axes" parameter is not used rait nau.
            extruder.set_position(newpos_e, homing_axes, self.print_time)
    
    def move(self, newpos, speed):
        """ToolHead.move() creates a Move() object with the parameters of the move (in cartesian space and in units of seconds and millimeters).

        Args:
            newpos (_type_): _description_
            speed (_type_): _description_
        """
        logging.info(f"\n\ntoolhead.move: moving to newpos={newpos}.\n\n")
        move = Move(toolhead=self, 
                    start_pos=self.commanded_pos,
                    end_pos=newpos, 
                    speed=speed)
        # NOTE: So far, the clock time for when this move
        #       will be sent are not known.
        # NOTE: Stepper move commands are not sent with
        #       a "clock" argument.

        # NOTE: Move checks.
        if not move.move_d:
            logging.info(f"\n\ntoolhead.move: early return, nothing to move. move.move_d={move.move_d}\n\n")
            return
        
        # NOTE: Kinematic move checks for XYZ and ABC axes.
        #       The check is skipped if the displacement vector is "small"
        #       (and thus is_kinematic_move is False, see the "Move" class above).
        if move.is_kinematic_move:
            # for axes in ["XYZ"]:
            for axes in list(self.kinematics):    
                # Iterate over["XYZ", "ABC"]
                logging.info("\n\n" + f"toolhead.move: check_move on {axes} move.\n\n")
                kin = self.kinematics[axes]
                kin.check_move(move)
            # self.kin.check_move(move)
            # TODO: implement move checks for ABC axes here too.
            # if self.abc_trapq is not None:
            #     self.kin_abc.check_move(move)
            
        # NOTE: Kinematic move checks for E axis.
        if move.axes_d[self.axis_count]:
            logging.info("\n\n" + f"toolhead.move: check_move on E move to {move.axes_d[self.axis_count]}.\n\n")
            self.extruder.check_move(move, e_axis=self.axis_count)
        
        # NOTE: Update "commanded_pos" with the "end_pos"
        #       of the current move command.
        self.commanded_pos[:] = move.end_pos
        
        # NOTE: Add the Move object to the MoveQueue.
        self.move_queue.add_move(move)
        
        if self.print_time > self.need_check_stall:
            self._check_stall()
    
    def manual_move(self, coord, speed):
        # NOTE: the "manual_move" command interprets "None" values
        #       as the latest (commanded) coordinates.
        
        # NOTE: get the current (last) position.
        curpos = list(self.commanded_pos)
        
        # NOTE: overwrite with the move's target postion.
        for i in range(len(coord)):
            if coord[i] is not None:
                curpos[i] = coord[i]
                
        # NOTE: send move.
        self.move(curpos, speed)
        
        # NOTE: This event is handled by "reset_last_position"
        #       (at gcode_move.py) which updates "self.last_position"
        #       in the GCodeMove class.
        self.printer.send_event(self.event_prefix + "manual_move")  # "toolhead:manual_move"
    
    def dwell(self, delay):
        # NOTE: get_last_move_time runs "_flush_lookahead" which then
        #       calls "flush" on the MoveQueue, and ends up calling 
        #       "_update_move_time", which updates "self.print_time".
        #       In essence "get_last_move_time" returns an updated
        #       "self.print_time". The delay is then added to it.
        next_print_time = self.get_last_move_time() + max(0., delay)
        self._update_move_time(next_print_time=next_print_time)
        self._check_stall()
    
    def wait_moves(self):
        
        # NOTE: Calls "move_queue.flush" unless in "special queuing state"
        #       (e.g. drip mode).
        # TODO: Check if this is the cause of the bug reported at Discord:
        #       https://discord.com/channels/431557959978450984/801826273227177984/1085312803558133800
        #       And fixed by an M400:
        #       https://discord.com/channels/431557959978450984/801826273227177984/1086104085201158260
        self._flush_lookahead()
        
        # NOTE: See "reactor.py"
        #       "Return the monotonic system time as a double"
        eventtime = self.reactor.monotonic()
        
        # NOTE: Loop while the queuing state is "regular" (e.g. not "drip"),
        #       or while the "print_time" is greater than the result of
        #       "mcu.estimated_print_time(eventtime)" (which converts "clock time"
        #       to "print time", see "clocksync.py").
        while (not self.special_queuing_state) or (self.print_time >= self.mcu.estimated_print_time(eventtime)):
            
            # NOTE: break the loop if the toolhead "cannot be paused".
            if not self.can_pause:
                break
            
            # NOTE: "pause" the reactor for a bit before looping again.
            #       This command does a bunch of undocumented stuff with
            #       greenlet objects, and may use "time.sleep" in some case.
            eventtime = self.reactor.pause(eventtime + 0.100)
    
    def set_extruder(self, extruder, extrude_pos):
        self.extruder = extruder
        self.commanded_pos[self.axis_count] = extrude_pos
    
    def get_extruder(self):
        return self.extruder
    
    # Homing "drip move" handling
    def _update_drip_move_time(self, next_print_time):
        # NOTE: called by "_process_moves" when in a "special_queuing_state"
        #       (i.e. when its value is not "" or None).
        flush_delay = DRIP_TIME + self.move_flush_time + self.kin_flush_delay
        while self.print_time < next_print_time:
            # NOTE: "drip_completion.test" is a method from "ReactorCompletion",
            #       but is beyond my understanding and deathwishes for spelunking.
            # NOTE: The "drip_completion" object was created by the "multi_complete"
            #       function at "homing.py", from a list of "wait" objects (returned
            #       by the "MCU_endstop.home_start" method, called during homing).
            # TODO: ask what it is for!
            if self.drip_completion.test():
                # NOTE: this "exception" does nothing, it "passes",
                #       but it is caught at the "drip_move" method,
                #       which runs "move_queue.reset" and "trapq_finalize_moves"
                #       in response. This must be an "alternate" way to break
                #       the while loop. A bit hacky though.
                raise DripModeEndSignal()
            curtime = self.reactor.monotonic()
            est_print_time = self.mcu.estimated_print_time(curtime)
            wait_time = self.print_time - est_print_time - flush_delay
            if wait_time > 0. and self.can_pause:
                # Pause before sending more steps
                self.drip_completion.wait(curtime + wait_time)
                continue
            npt = min(self.print_time + DRIP_SEGMENT_TIME, next_print_time)
            # NOTE: this updates "self.print_time" and calls "trapq_finalize_moves",
            #       possibly to "Generate steps for moves".
            self._update_move_time(next_print_time=npt)
            # NOTE: because how "print_time" is updated, the while loop will end
            #       before "self.print_time >= next_print_time" by "MOVE_BATCH_TIME".
    
    def drip_move(self, newpos, speed, drip_completion):
        self.dwell(self.kin_flush_delay)
        # Transition from "Flushed"/"Priming"/main state to "Drip" state
        self.move_queue.flush()
        self.special_queuing_state = "Drip"
        self.need_check_stall = self.reactor.NEVER
        self.reactor.update_timer(self.flush_timer, self.reactor.NEVER)
        self.move_queue.set_flush_time(self.buffer_time_high)
        self.idle_flush_print_time = 0.
        # NOTE: The "drip_completion=all_endstop_trigger" object is 
        #       probably made from "reactor.completion" objects.
        # NOTE: the "drip_completion.test" method will be used during
        #       the call to "_update_drip_move_time" during a homing move.
        self.drip_completion = drip_completion
        
        # Submit move
        try:
            # NOTE: uses "add_move", to add a move to the "move_queue".
            # NOTE: logging for tracing activity
            logging.info("\n\ndrip_move: sending move to the queue.\n\n")
            self.move(newpos, speed)
        except self.printer.command_error as e:
            self.flush_step_generation()
            raise
        
        # Transmit move in "drip" mode
        try:
            # NOTE: because the flush function is called with a 
            #       not None "special_queuing_state", the "_process_moves" 
            #       call will use "_update_drip_move_time".
            # NOTE: logging for tracing activity
            logging.info("\n\ndrip_move: flushing move queue / transmitting move.\n\n")
            self.move_queue.flush()
        except DripModeEndSignal as e:
            logging.info("\n\ndrip_move: resetting move queue / DripModeEndSignal caught.\n\n")
            
            # NOTE: deletes al moves in the queue
            self.move_queue.reset()
            
            # NOTE: "trapq_finalize_moves" calls a function in "trapq.c", described as:
            #       - Expire any moves older than `print_time` from the trapezoid velocity queue
            #       - Flush all moves from trapq (in the case of print_time=NEVER_TIME)
            #       I am guessing here that "older" means "with a smaller timestamp",
            #       or "previous". Otherwise it would not make sense.
            for axes in list(self.kinematics):
                # Iterate over ["XYZ", "ABC"].
                kin = self.kinematics[axes]
                logging.info(f"\n\nToolHead.drip_move calling trapq_finalize_moves on axes={axes} free_time=self.reactor.NEVER ({self.reactor.NEVER})\n\n")
                self.trapq_finalize_moves(kin.trapq, self.reactor.NEVER)
            
            # # NOTE: This calls a function in "trapq.c", described as:
            # #       - Expire any moves older than `print_time` from the trapezoid velocity queue
            # #       - Flush all moves from trapq (in the case of print_time=NEVER_TIME)
            # #       I am guessing here that "older" means "with a smaller timestamp",
            # #       otherwise it does not make sense.
            # self.trapq_finalize_moves(self.trapq, self.reactor.NEVER)
            
            # # NOTE: call trapq_finalize_moves on the ABC exes too.
            # self.trapq_finalize_moves(self.abc_trapq, self.reactor.NEVER)

            # NOTE: the above may be specific to toolhead and not to extruder...
            #       Add an "event" that calls this same method on the 
            #       extruder trapq as well.
            #self.printer.send_event("toolhead:trapq_finalize_extruder_drip_moves", 
            #                        self.reactor.NEVER, self.extruder.name)
            # NOTE: Alternatively, use the "update_move_time" of the extruder object.
            #       This function calls "trapq_finalize_moves(self.trapq, flush_time)"
            #       on the extruder's trapq.
            # TODO: Whether it will mess with XYZ-only homing or not remains to be tested.
            self.extruder.update_move_time(flush_time=self.reactor.NEVER)
        
        # Exit "Drip" state
        # NOTE: logging for tracing activity
        logging.info("\n\ndrip_move: calling flush_step_generation / exit drip state.\n\n")
        # NOTE: the "flush_step_generation" method, which calls:
        #       - "flush", which should do nothing (dine just above, and the queue is empty).
        #       - "reactor.update_timer"
        #       - "move_queue.set_flush_time"
        #       - "_update_move_time"
        # NOTE: pausing the program here prevented the "second home" move
        #       issue during homing the extruder with a drip move. The solution
        #       was to also call "trapq_finalize_moves" on the extruder's "trapq"
        #       above, and just before "flush_step_generation" below.
        self.flush_step_generation()
    
    # Misc commands
    def stats(self, eventtime):
        for m in self.all_mcus:
            m.check_active(self.print_time, eventtime)
        buffer_time = self.print_time - self.mcu.estimated_print_time(eventtime)
        is_active = buffer_time > -60. or not self.special_queuing_state
        if self.special_queuing_state == "Drip":
            buffer_time = 0.
        return is_active, "print_time=%.3f buffer_time=%.3f print_stall=%d" % (
            self.print_time, max(buffer_time, 0.), self.print_stall)
    def check_busy(self, eventtime):
        est_print_time = self.mcu.estimated_print_time(eventtime)
        lookahead_empty = not self.move_queue.queue
        return self.print_time, est_print_time, lookahead_empty
    def get_status(self, eventtime):
        print_time = self.print_time
        estimated_print_time = self.mcu.estimated_print_time(eventtime)
        res = dict(self.kin.get_status(eventtime))
        res.update({ 'print_time': print_time,
                     'stalls': self.print_stall,
                     'estimated_print_time': estimated_print_time,
                     'extruder': self.extruder.get_name(),
                     'position': self.Coord(*self.commanded_pos),
                     'max_velocity': self.max_velocity,
                     'max_accel': self.max_accel,
                     'max_accel_to_decel': self.requested_accel_to_decel,
                     'square_corner_velocity': self.square_corner_velocity})
        return res
    
    def _handle_shutdown(self):
        self.can_pause = False
        self.move_queue.reset()
    
    def get_kinematics(self, axes="XYZ"):
        if axes == "XYZ":
            return self.kinematics[axes]
        elif axes == "ABC":
            return self.kinematics[axes]
        else:
            logging.info("No kinematics matched to axes={axes} returning toolhead.kin (legacy behaviour)")
            return self.kin
    def get_kinematics_abc(self):
        # TODO: update the rest of the code to use "get_trapq" with "axes" instead.
        return self.kin_abc
    
    def get_trapq(self, axes="XYZ"):
        if axes == "XYZ":
            return self.kinematics[axes].trapq
        elif axes == "ABC":
            return self.kinematics[axes].trapq
        else:
            logging.info("No kinematics matched to axes={axes} returning toolhead.trapq (legacy behaviour)")
            return self.trapq
    def get_abc_trapq(self):
        # TODO: update the rest of the code to use "get_trapq" with "axes" instead.
        return self.abc_trapq
    
    def register_step_generator(self, handler):
        self.step_generators.append(handler)
    def note_step_generation_scan_time(self, delay, old_delay=0.):
        self.flush_step_generation()
        cur_delay = self.kin_flush_delay
        if old_delay:
            self.kin_flush_times.pop(self.kin_flush_times.index(old_delay))
        if delay:
            self.kin_flush_times.append(delay)
        new_delay = max(self.kin_flush_times + [SDS_CHECK_TIME])
        self.kin_flush_delay = new_delay
    def register_lookahead_callback(self, callback):
        last_move = self.move_queue.get_last()
        if last_move is None:
            callback(self.get_last_move_time())
            return
        last_move.timing_callbacks.append(callback)
    def note_kinematic_activity(self, kin_time):
        self.last_kin_move_time = max(self.last_kin_move_time, kin_time)
    def get_max_velocity(self):
        return self.max_velocity, self.max_accel
    def _calc_junction_deviation(self):
        scv2 = self.square_corner_velocity**2
        self.junction_deviation = scv2 * (math.sqrt(2.) - 1.) / self.max_accel
        self.max_accel_to_decel = min(self.requested_accel_to_decel,
                                      self.max_accel)
    def cmd_G4(self, gcmd):
        # Dwell
        delay = gcmd.get_float('P', 0., minval=0.) / 1000.
        self.dwell(delay)
    def cmd_M400(self, gcmd):
        # Wait for current moves to finish
        self.wait_moves()
    cmd_SET_VELOCITY_LIMIT_help = "Set printer velocity limits"
    def cmd_SET_VELOCITY_LIMIT(self, gcmd):
        max_velocity = gcmd.get_float('VELOCITY', None, above=0.)
        max_accel = gcmd.get_float('ACCEL', None, above=0.)
        square_corner_velocity = gcmd.get_float(
            'SQUARE_CORNER_VELOCITY', None, minval=0.)
        requested_accel_to_decel = gcmd.get_float(
            'ACCEL_TO_DECEL', None, above=0.)
        if max_velocity is not None:
            self.max_velocity = max_velocity
        if max_accel is not None:
            self.max_accel = max_accel
        if square_corner_velocity is not None:
            self.square_corner_velocity = square_corner_velocity
        if requested_accel_to_decel is not None:
            self.requested_accel_to_decel = requested_accel_to_decel
        self._calc_junction_deviation()
        msg = ("max_velocity: %.6f\n"
               "max_accel: %.6f\n"
               "max_accel_to_decel: %.6f\n"
               "square_corner_velocity: %.6f" % (
                   self.max_velocity, self.max_accel,
                   self.requested_accel_to_decel,
                   self.square_corner_velocity))
        self.printer.set_rollover_info("toolhead", self.event_prefix + " %s" % (msg,))
        if (max_velocity is None and
            max_accel is None and
            square_corner_velocity is None and
            requested_accel_to_decel is None):
            gcmd.respond_info(msg, log=False)
    def cmd_M204(self, gcmd):
        # Use S for accel
        accel = gcmd.get_float('S', None, above=0.)
        if accel is None:
            # Use minimum of P and T for accel
            p = gcmd.get_float('P', None, above=0.)
            t = gcmd.get_float('T', None, above=0.)
            if p is None or t is None:
                gcmd.respond_info('Invalid M204 command "%s"'
                                  % (gcmd.get_commandline(),))
                return
            accel = min(p, t)
        self.max_accel = accel
        self._calc_junction_deviation()

def load_config_prefix(config):
    config.get_printer().add_object('toolhead_stepper', ToolHeadStepper(config))
    kinematics.extruder.add_printer_objects(config)
