# print_start_abort.py — let a Mainsail/KlipperScreen "Cancel" interrupt PRINT_START fast.
#
# Installed as a symlink: ~/klipper/klippy/extras/print_start_abort.py ->
#   ~/printer_data/config/klippy_extras/print_start_abort.py   (versioned in micron-config)
# Config: [print_start_abort] (declared in macros/print_start.cfg).
#
# PROBLEM: Klipper runs a whole gcode_macro (nested calls included) while holding the gcode
# mutex. Moonraker's cancel goes through the "pause_resume/cancel" webhook, which runs
# CANCEL_PRINT via gcode.run_script() and therefore queues on that mutex until PRINT_START
# returns — 10-20 min during heat-soak/QGL/mesh.
#
# FIX (2 parts):
#  1. Wrap the "pause_resume/cancel" webhook. While PRINT_START has us ARMED, a cancel request
#     first sets `requested`, then falls through to the stock handler (which still queues on
#     the mutex and runs the real CANCEL_PRINT as soon as PRINT_START returns — ReactorMutex
#     hands the lock straight to the next queued waiter, so the file can't run another line
#     first).
#  2. Interruptible versions of PRINT_START's long waits, which return within ~0.5s once
#     `requested` is set. PRINT_START's stage macros check printer.print_start_abort.requested
#     at expansion and skip themselves, so the macro unwinds in seconds.
#  When NOT armed, the wait commands behave exactly like their stock counterparts, so they are
#  safe to use outside PRINT_START too (e.g. _CHAMBER_HEATSOAK_WAIT via HEATSOAK_CHAMBER).
#
# Commands:
#   PS_ABORT_ARM                       arm + clear request (start of PRINT_START)
#   PS_ABORT_DISARM                    disarm + clear request (end of PRINT_START, cancel cleanup)
#   PS_WAIT_HEATER HEATER=<name>       = the wait half of M109/M190 (heater.check_busy)
#   PS_WAIT_TEMP SENSOR=<name> [MINIMUM=] [MAXIMUM=]   = TEMPERATURE_WAIT
#   PS_DWELL SECONDS=<s>               = M400 + real-time dwell
#
# Only intercepts the webhook (UI Cancel buttons). A CANCEL_PRINT typed into the console
# arrives via gcode/script and still waits for PRINT_START, as before.

import logging

POLL = 0.5


class PrintStartAbort:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.armed = False
        self.requested = False
        self._orig_cancel = None
        self.printer.register_event_handler("klippy:connect", self._wrap_cancel)
        for cmd in ('PS_ABORT_ARM', 'PS_ABORT_DISARM', 'PS_WAIT_HEATER',
                    'PS_WAIT_TEMP', 'PS_DWELL'):
            self.gcode.register_command(cmd, getattr(self, 'cmd_' + cmd))

    # ---- webhook wrap -------------------------------------------------------------
    def _wrap_cancel(self):
        webhooks = self.printer.lookup_object('webhooks')
        endpoints = getattr(webhooks, '_endpoints', None)
        if not endpoints or 'pause_resume/cancel' not in endpoints:
            # Klipper internals changed: fail safe (old slow-cancel behaviour), but say so.
            logging.warning("print_start_abort: pause_resume/cancel endpoint not found; "
                            "fast PRINT_START cancel disabled")
            return
        self._orig_cancel = endpoints['pause_resume/cancel']
        endpoints['pause_resume/cancel'] = self._handle_cancel
        logging.info("print_start_abort: wrapped pause_resume/cancel")

    def _handle_cancel(self, web_request):
        if self.armed and not self.requested:
            self.requested = True
            self.gcode.respond_info(
                "Cancel requested during PRINT_START - aborting start-up")
        return self._orig_cancel(web_request)

    # ---- helpers ------------------------------------------------------------------
    def _abort(self):
        return self.armed and self.requested

    def _temp_report(self, eventtime):
        pheaters = self.printer.lookup_object('heaters')
        get = getattr(pheaters, '_get_temp', None)
        if get is not None:
            self.gcode.respond_raw(get(eventtime))

    # ---- commands -----------------------------------------------------------------
    def cmd_PS_ABORT_ARM(self, gcmd):
        self.armed = True
        self.requested = False

    def cmd_PS_ABORT_DISARM(self, gcmd):
        self.armed = False
        self.requested = False

    def cmd_PS_WAIT_HEATER(self, gcmd):
        pheaters = self.printer.lookup_object('heaters')
        heater = pheaters.lookup_heater(gcmd.get('HEATER'))
        if self.printer.get_start_args().get('debugoutput') is not None:
            return
        toolhead = self.printer.lookup_object('toolhead')
        eventtime = self.reactor.monotonic()
        next_report = eventtime
        while (not self.printer.is_shutdown() and not self._abort()
               and heater.check_busy(eventtime)):
            toolhead.get_last_move_time()
            if eventtime >= next_report:
                self._temp_report(eventtime)
                next_report = eventtime + 1.
            eventtime = self.reactor.pause(eventtime + POLL)

    def cmd_PS_WAIT_TEMP(self, gcmd):
        sensor_name = gcmd.get('SENSOR')
        min_temp = gcmd.get_float('MINIMUM', float('-inf'))
        max_temp = gcmd.get_float('MAXIMUM', float('inf'), above=min_temp)
        if min_temp == float('-inf') and max_temp == float('inf'):
            raise gcmd.error("PS_WAIT_TEMP: missing MINIMUM or MAXIMUM")
        if self.printer.get_start_args().get('debugoutput') is not None:
            return
        pheaters = self.printer.lookup_object('heaters')
        if sensor_name in getattr(pheaters, 'heaters', {}):
            sensor = pheaters.heaters[sensor_name]
        else:
            sensor = self.printer.lookup_object(sensor_name)
        toolhead = self.printer.lookup_object('toolhead')
        eventtime = self.reactor.monotonic()
        next_report = eventtime
        while not self.printer.is_shutdown() and not self._abort():
            temp, target = sensor.get_temp(eventtime)
            if min_temp <= temp <= max_temp:
                return
            toolhead.get_last_move_time()
            if eventtime >= next_report:
                self._temp_report(eventtime)
                next_report = eventtime + 1.
            eventtime = self.reactor.pause(eventtime + POLL)

    def cmd_PS_DWELL(self, gcmd):
        seconds = gcmd.get_float('SECONDS', minval=0.)
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.wait_moves()
        end = self.reactor.monotonic() + seconds
        eventtime = self.reactor.monotonic()
        while (eventtime < end and not self.printer.is_shutdown()
               and not self._abort()):
            eventtime = self.reactor.pause(min(end, eventtime + POLL))

    def get_status(self, eventtime):
        return {'armed': self.armed, 'requested': self.requested}


def load_config(config):
    return PrintStartAbort(config)
