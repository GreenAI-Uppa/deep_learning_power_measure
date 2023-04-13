"""
this module contains mainly two classes
    - Experiment is an entry point to start and end the recording of the power consumption of your Experiment
    - ExpResult is used to process and format the recordings.

Both classes uses a driver attribute to communicate with a database, or read and write in json files
"""
from functools import wraps
import os
import sys
import traceback
from multiprocessing import Process, Queue
from queue import Empty as EmptyQueueException
import time
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import datetime
import psutil
from . import rapl_power
from . import gpu_power
from . import model_complexity
import signal

STOP_MESSAGE = "Stop"

def joules_to_wh(n):
    """conversion function"""
    if hasattr(n, '__iter__'):
        return [i*3600/1000 for i in n]
    return n*3600/1000

def integrate(metric, start=None, end=None, allow_None=False):
    """integral of the metric values over time"""
    r = [0]
    if start is None:
        start = 0
    if end is None:
        end = len(metric)-1
    for i in range(start, end):
        x1 = metric[i]['date']
        x2 = metric[i+1]['date']
        y1 = metric[i]['value']
        y2 = metric[i+1]['value']
        if y1 is None or y2 is None:
            if allow_None:
                r.append(None)
                continue
            else:
                return None
        v = (x2-x1)*(y2+y1)/2
        v += r[-1]
        r.append(v)
    return r

def get_pid_list(current_pid):
    current_process = psutil.Process(os.getppid())
    pid_list = [current_process.pid] + [
        child.pid for child in current_process.children(recursive=True)
    ]
    # removing the pids from the measurement AIPowerMeter functions
    queue_process = psutil.Process(current_pid)
    queue_pids = [queue_process.pid] + [child.pid for child in queue_process.children(recursive=True)]
    for queue_pid in queue_pids:
        if queue_pid in pid_list:
            pid_list.remove(queue_pid)
    return pid_list

def average(metric, start=None, end=None):
    """average of the metric values over time"""
    if start is None:
        start = 0
    if end is None:
        end = len(metric)-1
    r = integrate(metric, start=start, end=end)
    if len(metric) == 1:
        return r[0]
    return r[-1]/(metric[end]['date'] - metric[start]['date'])

def total(metric, start=None, end=None):
    r = integrate(metric, start=start, end=end)
    return r[-1]

def interpolate(metric1, metric2):
    """
    return two new metrics so that metric1 and metric2 have the same range of dates
    """
    x1 = [m['date'] for m in metric1]
    x2 = [m['date'] for m in metric2]
    x = sorted( set(x1 + x2))
    y1 = [m['value'] for m in metric1]
    y2 = [m['value'] for m in metric2]
    y1 = np.interp(x, x1, y1)
    y2 = np.interp(x, x2, y2)
    metric1 = [{'date':x, 'value':v} for (x,v) in zip(x, y1) ]
    metric2 = [{'date':x, 'value':v} for (x,v) in zip(x, y2) ]
    return metric1, metric2

def humanize_bytes(num, suffix='B'):
    """
    convert a float number to a human readable string to display a number of bytes
    (copied from https://stackoverflow.com/questions/1094841/get-human-readable-version-of-file-size)
    """
    for unit in ['','Ki','Mi','Gi','Ti','Pi','Ei','Zi']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)

def time_to_sec(t):
    """convert a date to timestamp in seconds"""
    return t.timestamp()

def cumsum(metric):
    """simple wrapper to cumsum"""
    return np.cumsum([ m['value'] for m in metric ])

def processify(func):
    """Decorator to run a function as a process.
    The created process is joined, so the code does not
    run in parallel.
    """
    def process_func(self, queue, *args, **kwargs):
        try:
            ret = func(self, queue, *args, **kwargs)
        except Exception as e:
            ex_type, ex_value, tb = sys.exc_info()
            error = ex_type, ex_value, "".join(traceback.format_tb(tb))
            ret = None
            queue.put((ret, error))
            raise e
        else:
            error = None
        queue.put((ret, error))

    # register original function with different name
    # in sys.modules so it is pickable
    process_func.__name__ = func.__name__ + "processify_func"
    setattr(sys.modules[__name__], process_func.__name__, process_func)

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        queue = Queue()  # not the same as a Queue.Queue()
        p = Process(target=process_func, args=[self, queue] + list(args), kwargs=kwargs)
        p.start()
        queue.put(p.pid)
        return p, queue
    return wrapper


class Experiment():
    """
    This class provides the method to start an experiment
    by launching a thread which will record the power draw.
    The recording can be ended by sending a stop message
    to this thread
    """
    def __init__(self, driver, model=None, input_size=None, cont=False):
        """
        wrapper class with the methods in charge of recording
        cpu and gpu uses

        driver: interface with a database to save the results
        model and input_size : to compute a model card which will summarize the model
            model is a pytorch model from nn.Module, and input_size is a tuple of int,
            for instance, you can provide  input_size = (64, 3, 32, 32) if you are processing
            3 channels images with a batch size of 64.
        cont : whether or not to erase existing results or append new results.
        """
        self.db_driver = driver
        if not cont:
            driver.erase()
        if model is not None:
            # attempting to guess the device on the model.
            device = next(model.parameters()).device
            model.to(device)
            self.save_model_card(model, input_size, device=device)
        #self.power_meter_available = is_omegawatt_available CHECK IF BINARY PRESENT
        self.rapl_available, msg_rapl = rapl_power.is_rapl_compatible()
        self.nvidia_available, msg_nvidia = gpu_power.is_nvidia_compatible()
        self.wattmeter_available = os.path.isfile(self.db_driver.wattemeter_exec)
        if not self.rapl_available and not self.nvidia_available:
            raise Exception(
            "\n\n Neither rapl and nvidia are available, I can't measure anything.\n\n "
            + msg_rapl + "\n\n"
            + msg_nvidia
            )
        if not self.rapl_available:
            print("RAPL not available: " + msg_rapl)
        else:
            print(msg_rapl)
        if self.wattmeter_available:
            print("wattmeter available at: "+self.db_driver.wattemeter_exec)
        else:
            print("power meter not avaible: "+self.db_driver.wattemeter_exec," does not exist")
        if not self.nvidia_available:
            print("nvidia not available: " + msg_nvidia)
        else:
            print(msg_nvidia)
            self.gpu_logs = []
            self.min_gpu_powers = gpu_power.get_min_power()
            self.pid_per_gpu = {} # {gpu_id : {pid, last_time_active}}


    def log_usage(self, metric_gpu, pid_list, time_window=3, waiting_phase=20):
        """
        record the last gpu usage, and remove the usage after a

        time_window : in seconds : after this duration, we remove recordings
        from the log
        waiting_phase : in seconds. The gpu remains under high voltage idle state
        during this period after the last pid has finished its computation
        """
        now = time.time()
        log = {"timestamp": now }
        log["per_gpu_power_draw"] = metric_gpu["per_gpu_power_draw"]
        log["per_gpu_estimated_attributable_utilization"] = metric_gpu["per_gpu_estimated_attributable_utilization"]
        self.gpu_logs = [t for t in self.gpu_logs if now - t['timestamp'] < time_window]
        self.gpu_logs.append(log)

        # update the list of pids running on the different gpus
        # remove the ones older than 20 seconds, because at that time, this pid does not have an influence on the consumption anymore
        for gpu_id, pid_cats in self.pid_per_gpu.items():
            for cat, pid_last_times in pid_cats.items():
                for pid in list(pid_last_times):
                    last_seen = pid_last_times[pid]
                    if now - last_seen > waiting_phase:
                        del pid_last_times[pid]
        # add the pids running at the moment
        for gpu_id, usage in metric_gpu["per_gpu_per_pid_utilization_absolute"].items():
            for pid, u in usage.items():
                if u == 0:
                    continue
                if gpu_id not in self.pid_per_gpu:
                    self.pid_per_gpu[gpu_id] = {'pid_this_exp':{}, 'other_pids':{}}
                if pid in pid_list:
                    self.pid_per_gpu[gpu_id]['pid_this_exp'][pid] = now
                else:
                    self.pid_per_gpu[gpu_id]['other_pids'][pid] = now

    def allocate_gpu_power(self, per_gpu_power_draw):
        """
        computing attributable power and use
        We average the gpu use and power over a time window and share it among
        the different processes

        There is a fix power that is used by the gpu as soon as it is used. This part
        is shared equally among all the active pids regardless of their amount of
        stream multiprocessor used

        Note that the gpu is set at this mininum voltage for a time window of
        around 20 seconds after inactivity.
        """
        per_gpu_attributable_power = {}
        per_gpu_attributable_sm_use = {}
        if len(self.pid_per_gpu) == 0:
            return {'all': 0}, {}
        for gpu_id in self.pid_per_gpu:
            this_gpu_power_draw = per_gpu_power_draw[gpu_id]
            use_curve =  [ {'date': t['timestamp'], 'value': t['per_gpu_estimated_attributable_utilization'][gpu_id] } for t in self.gpu_logs ]
            this_gpu_relative_use = average(use_curve) # average use of the gpu over the time window
            per_gpu_attributable_sm_use[gpu_id] = this_gpu_relative_use
            all_pids_on_this_gpu = len(self.pid_per_gpu[gpu_id]['pid_this_exp']) + len(self.pid_per_gpu[gpu_id]['other_pids'])
            if all_pids_on_this_gpu == 0:
                prop_active_pid = 0
            else:
                prop_active_pid = len(self.pid_per_gpu[gpu_id]['pid_this_exp']) / all_pids_on_this_gpu
            usage_power = (this_gpu_power_draw - self.min_gpu_powers[gpu_id])
            fix_power = self.min_gpu_powers[gpu_id] * prop_active_pid
            per_gpu_attributable_power[gpu_id] = usage_power * this_gpu_relative_use + fix_power
        per_gpu_attributable_power['all'] = sum(per_gpu_attributable_power.values())
        return per_gpu_attributable_power, per_gpu_attributable_sm_use

    def save_model_card(self, model, input_size, device='cpu'):
        """
        get a model summary and save it

        model : pytorch model
        input_size : input_size for this model (batch_size, *input_data_size)
        """
        if model is None:
            raise Exception('You tried to compute the model card with the parameter model set to None')

        if model is not None and input_size is None:
            raise Exception('a model was given as parameter, but the input_size argument must also be supplied to estimate the model card')
        summary = model_complexity.get_summary(model, input_size, device=device)
        self.db_driver.save_model_card(summary)

    @processify
    def measure_from_pid_list(self, queue, pid_list, period=1):
        """record power use for the processes given in pid_list"""
        self.measure(queue, pid_list, period=period)

    @processify
    def measure_yourself(self, queue, period=1):
        """
        record power use for the process which calls this method
        """
        current_pid = queue.get()
        self.measure(queue, None, current_pid=current_pid, period=period)

    def measure(self, queue, pid_args, current_pid = None, period=1):
        """
        performs power use recording

        queue : queue used to communicate to the thread which ask
        for the recording
        pid_list : the recording will be done for these process
        period : waiting time between two RAPL samples.
        """
        time_at_last_measure = 0
        if self.wattmeter_available:
            ## launch power meter recording
            ## logfile = self.wattmeter_logfile
            proc = self.db_driver.save_wattmeter_metrics()
        while True:
            if pid_args is None:
                # will obtain the pid from the parents, ie the script from which the measure function has been called
                pid_list = get_pid_list(current_pid)
            else:
                # the user specified a set of process he wants to monitor
                pid_list = pid_args
            # there have a buffer and allocate per pid with lifo
            # with time
            metrics = {}
            if self.nvidia_available:
                # launch in separate threads because they won't have the same frequency
                metrics_gpu = gpu_power.get_nvidia_gpu_power(pid_list)
                self.log_usage(metrics_gpu, pid_list)
            if self.rapl_available:
                metrics['cpu'] = rapl_power.get_metrics(pid_list, period=0.1)
            if time.time() - time_at_last_measure > period:
                time_at_last_measure = time.time()
                if self.nvidia_available:
                    per_gpu_attributable_power, _ = self.allocate_gpu_power(metrics_gpu['per_gpu_power_draw'])
                    metrics_gpu['per_gpu_attributable_power'] = per_gpu_attributable_power
                    metrics['gpu'] = metrics_gpu
                self.db_driver.save_power_metrics(metrics)
            try:
                message = queue.get(block=False)
                # so there are two types of expected messages.
                # The STOP message which is a string, and the metrics dictionnary that this function is sending
                if message == STOP_MESSAGE:
                    print("Done with measuring")
                    if self.wattmeter_available  and proc:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    # os.system("kill -10 `cat /tmp/pid`")
                    return
            except EmptyQueueException:
                pass
        

class ExpResults():
    """
    Process the power recording from an experiment.
    The actual reading of the recording is done by the fb_driver attribute.
    """
    def __init__(self, db_driver):
        self.db_driver = db_driver
        self.cpu_metrics, self.gpu_metrics, self.exp_metrics, self.wattmeter_metrics = self.db_driver.load_metrics()
        if self.cpu_metrics is None and self.gpu_metrics is None and self.exp_metrics is None:
            raise Exception('I could not load any recordings from folder: "' +
            self.db_driver.folder +
            '".\n Please check that the folder contains valid recordings')
        self.model_card = self.db_driver.get_model_card()

    def list_metrics(self):
        metrics = {'CPU':[],'GPU':[],'Experiment':[],'wattmeter':[]}
        if self.cpu_metrics is not None:
            for k in self.cpu_metrics:
                metrics['CPU'].append(k)
        if self.gpu_metrics is not None:
            for k in self.gpu_metrics:
                metrics['GPU'].append(k)
        if self.exp_metrics is not None:
            for k in self.exp_metrics:
                metrics['Experiment'].append(k)
        if self.wattmeter_metrics is not None:
            for k in self.wattmeter_metrics:
                metrics['wattmeter'].append(k)
        return metrics

    def print_metrics(self):
        metrics = self.list_metrics()
        for k, mets in metrics.items():
            print(k)
            if len(mets) == 0:
                print('      NOT AVAILABLE')
            else:
                print(mets)

    def get_curve(self, name):
        """
        name : key to one of the metric dictionnaries

        return a series of data points [{ "date": date1, "value" : value1}, {"date": date2 ,... }]
        where the dates are in seconds and the value unit is taken from the dictionnaries without transformation.
        """
        curve = None
        if self.cpu_metrics is not None:
            if name in self.cpu_metrics:
                curve = [{'date':time_to_sec(x), 'value':v} for (x,v) in zip(self.cpu_metrics[name]['dates'], self.cpu_metrics[name]['values']) if v is not None]

        if self.gpu_metrics is not None:
            if name in self.gpu_metrics:
                if "dates" in self.gpu_metrics[name]:
                    curve = [{'date':time_to_sec(x), 'value':v} for (x,v) in zip(self.gpu_metrics[name]['dates'], self.gpu_metrics[name]['values']) if v is not None]
                else:
                    curve = {}
                    for device_id, metric in self.gpu_metrics[name].items():
                        curve[device_id] = [{'date':time_to_sec(x), 'value':v} for (x,v) in zip(self.gpu_metrics[name][device_id]['dates'], self.gpu_metrics[name][device_id]['values']) if v is not None]

        if self.exp_metrics is not None:
            if name in self.exp_metrics:
                curve = [{'date':time_to_sec(x), 'value':v} for (x,v) in zip(self.exp_metrics[name]['dates'], self.exp_metrics[name]['values']) if v is not None]
        if self.wattmeter_metrics is not None:
            if name in self.wattmeter_metrics:
                curve = [{'date':x, 'value':v} for (x,v) in zip(self.wattmeter_metrics[name]['dates'], self.wattmeter_metrics[name]['values']) if v is not None]

        if curve is not None and len(curve) == 0:
            curve = None
        return curve

    def get_exp_duration(self):
        if self.cpu_metrics is not None:
            for name, recordings in self.cpu_metrics.items():
                times = sorted([time_to_sec(date)  for date in recordings['dates']])
                return times[-1] - times[0]

        if self.gpu_metrics is not None:
            for name, recordings in self.gpu_metrics.items():
                times = sorted([time_to_sec(date)  for date in recordings['dates']])
                return times[-1] - times[0]

        if self.exp_metrics is not None:
            for name, recordings in self.exp_metrics.items():
                times = sorted([time_to_sec(date)  for date in recordings['dates']])
                return times[-1] - times[0]
        return -1

    def total_(self, metric_name: str):
        """Return the integration over time for the metric. For instance if the metric is in watt and the time in seconds,
        the return value is the energy consumed in Joules"""
        metric = self.get_curve(metric_name)
        if metric is None:
            return None
        elif isinstance(metric, list):
            r =integrate(metric)
            if r is None:
                return r
            return r[-1]
        else:
            totals = {}
            for device_id, mtrc in metric.items():
                r = integrate(mtrc)
                if r is not None:
                    r = r[-1]
                totals[device_id] = r
            return totals

    def average_(self, metric_name: str):
        """take the average of a metric"""
        metric = self.get_curve(metric_name)
        if metric is None:
            return None
        elif isinstance(metric, list):
            r = integrate(metric)
            if r is None:
                return r
            r = r[-1]
            if len(metric) == 1:
                return r
            return r /( metric[-1]['date'] - metric[0]['date'])
        else:# compute the average for each device
            averages = {}
            for device_id, mtrc in metric.items():
                r = integrate(mtrc)
                if r is not None:
                    r = r[-1]
                    if len(mtrc) > 1:
                        r = r /( mtrc[-1]['date'] - mtrc[0]['date'])
                averages[device_id] = r
            return averages

    def max_(self, metric_name: str):
        """return the max of a metric"""
        metric = self.get_curve(metric_name)
        if metric is None:
            return None
        elif isinstance(metric, list):
            return max([m["value"] for m in metric])
        else:
            maxs = {}
            for device_id, mtrc in metric.items():
                if mtrc is None or len(mtrc)==0:
                    maxs[device_id] = None
                else:
                    maxs[device_id] = max([m["value"] for m in mtrc])
            return maxs

    def total_power_draw(self):
        """extracting cpu and GPU power draw"""
        total_intel_power = self.total_('intel_power')
        abs_nvidia_power = self.total_('nvidia_draw_absolute')
        return total_intel_power + abs_nvidia_power

    def display_curves(self, metric_names):
        """
        Input:
          metric_names [metric_name1, metric_name2,...]
          see print_metrics() function for what's available 
        """
        fig, ax = plt.subplots()
        for metric_name in metric_names:
            curve = self.get_curve(metric_name)
            if curve is None:
                continue
            if isinstance(curve,list):
                df = pd.DataFrame(curve)
                df['date_datetime'] = [ datetime.datetime.fromtimestamp(d) for d in df['date'] ]
                df['date_datetime'] = pd.to_datetime(df['date_datetime'])
                ax.plot(df['date_datetime'], df['value'], label=metric_name)
            else: # compute the average for each device
                for device_id, metric in curve.items():
                    df = pd.DataFrame(metric)
                    df['date_datetime'] = [ datetime.datetime.fromtimestamp(d) for d in df['date'] ]
                    df['date_datetime'] = pd.to_datetime(df['date_datetime'])
                    ax.plot(df['date_datetime'], df['value'],label=metric_name+":"+device_id)
        ax.format_xdata = mdates.DateFormatter('%H:%M:%S')
        plt.xticks(rotation=45)
        plt.legend()
        plt.show()


    def display_2_curves(self, metric_name1, metric_name2):
        """
        """
        fig, ax = plt.subplots()
        curve = self.get_curve(metric_name1)
        #if curve is None:
        #    raise Exception('invalide metric name')
        if isinstance(curve,list):
            df = pd.DataFrame(curve)
            df['date_datetime'] = [ datetime.datetime.fromtimestamp(d) for d in df['date'] ]
            df['date_datetime'] = pd.to_datetime(df['date_datetime'])
            ax.plot(df['date_datetime'], df['value'], label=metric_name1)
            ax.set_ylabel(metric_name1, color="blue",fontsize=14)
        else:
            for device_id, metric in curve.items():
                df = pd.DataFrame(metric)
                df['date_datetime'] = [ datetime.datetime.fromtimestamp(d) for d in df['date'] ]
                df['date_datetime'] = pd.to_datetime(df['date_datetime'])
                ax.plot(df['date_datetime'], df['value'],label=metric_name1+":"+device_id)
                ax.set_ylabel(metric_name1+":"+device_id, color="blue",fontsize=14)
        ax.format_xdata = mdates.DateFormatter('%H:%M:%S')

        ax2 = ax.twinx()
        curve = self.get_curve(metric_name2)
        if isinstance(curve,list):
            df = pd.DataFrame(curve)
            df['date_datetime'] = [ datetime.datetime.fromtimestamp(d) for d in df['date'] ]
            df['date_datetime'] = pd.to_datetime(df['date_datetime'])
            ax2.plot(df['date_datetime'], df['value'], label=metric_name2, color="red")
            ax2.set_ylabel(metric_name2, color="red",fontsize=14)
        else:
            for device_id, metric in curve.items():
                df = pd.DataFrame(metric)
                df['date_datetime'] = [ datetime.datetime.fromtimestamp(d) for d in df['date'] ]
                df['date_datetime'] = pd.to_datetime(df['date_datetime'])
                ax2.plot(df['date_datetime'], df['value'],label=metric_name2+":"+device_id, color='red')
                ax2.set_ylabel(metric_name2+":"+device_id, color="red",fontsize=14)
        ax2.format_xdata = mdates.DateFormatter('%H:%M:%S')
        plt.xticks(rotation=45)
        plt.show()

    def __str__(self) -> str:
        r = ['Available metrics : ']
        r.append('CPU')
        if self.cpu_metrics is not None:
            r.append('  '+','.join([k for k in self.cpu_metrics.keys()]))
        else:
            r.append('NOT AVAILABLE')
        r.append('GPU')
        if self.gpu_metrics is not None:
            r.append('  '+','.join([k for k in self.gpu_metrics.keys()]))
        else:
            r.append('NOT AVAILABLE')
        r.append('Experiments')
        if self.gpu_metrics is not None:
            r.append('  '+','.join([k for k in self.gpu_metrics.keys()]))
        else:
            r.append('NOT AVAILABLE')
        r.append('\n\ncall print() method to display power consumption')
        return '\n'.join(r)
    
    def print(self):
        """
        simple print of the experiment summary
        """
        print("============================================ EXPERIMENT SUMMARY ============================================")
        if self.model_card is not None and 'total_params' in self.model_card and 'total_mult_adds' in self.model_card:
            print(self.model_card)
            print("MODEL SUMMARY: ", self.model_card['total_params'],"parameters and ",self.model_card['total_mult_adds'], "mac operations during the forward pass of your model")
            print()

        print('Experiment duration: ', self.get_exp_duration(), 'seconds')
        if self.cpu_metrics is not None:
            print("ENERGY CONSUMPTION: ")
            print("on the cpu")
            print()
            total_psys_power = self.total_('psys_power')
            total_intel_power = self.total_('intel_power')
            total_dram_power = self.total_('total_dram_power')
            total_cpu_power = self.total_('total_cpu_power')
            rel_dram_power = self.total_('per_process_dram_power')
            rel_cpu_power = self.total_('per_process_cpu_power')
            mem_use_abs = self.average_('per_process_mem_use_abs')
            mem_use_uss = self.average_('per_process_mem_use_uss')
            if total_dram_power is None and mem_use_abs is None:
                print("RAM consumption not available. RAM usage not available")
            elif total_dram_power is None:
                print("RAM consumption not available. Your usage was ",humanize_bytes(mem_use_abs), 'with an overhead of',humanize_bytes(mem_use_uss))
            else:
                print("Total RAM consumption:", total_dram_power, "joules, your experiment consumption: ", rel_dram_power, "joules, for an average of",humanize_bytes(mem_use_abs), 'with an overhead of',humanize_bytes(mem_use_uss))
            if total_cpu_power is None:
                print("CPU consumption not available")
            else:
                print("Total CPU consumption:", total_cpu_power, "joules, your experiment consumption: ", rel_cpu_power, "joules")
            print("total intel power: ", total_intel_power, "joules")
            print("total psys power: ",total_psys_power, "joules")
        if self.gpu_metrics is not None:
            print()
            print()
            print("GPU")
            abs_nvidia_power = self.total_('nvidia_draw_absolute')
            rel_nvidia_power = self.total_('nvidia_attributable_power')
            print("nvidia total consumption:",abs_nvidia_power, "joules, your consumption: ",rel_nvidia_power,"joules")
            nvidia_mem_use_abs = self.max_("nvidia_mem_use")
            print('Max memory used:')
            for device_id, mx in nvidia_mem_use_abs.items():
                if mx is None:
                    print('    gpu:',device_id, 'memory used not available')
                else:
                    print('    gpu:',device_id,":", humanize_bytes(mx))
            nvidia_average_sm = self.average_("nvidia_sm_use")
            print('Average GPU usage:')
            for device_id, mx in nvidia_average_sm.items():
                if mx is None:
                    print('    gpu:',device_id, 'sm usage not available')
                else:
                    print('    gpu: {}: {:0.3f} %'.format(device_id, mx*100))
            per_gpu_attributable_power = self.total_('per_gpu_attributable_power')
            print('Attributable usage per GPU')
            for device_id, mx in per_gpu_attributable_power.items():
                if device_id == 'all':
                    continue
                if mx is None:
                    print('    gpu:',device_id, 'power draw not available')
                else:
                    print('    gpu: {}: {:0.3f} joules'.format(device_id, mx))
            
        if self.wattmeter_metrics is not None:
            print()
            print()
            print("Recorded by the wattmeter")
            pow_machine1 = self.total_('#activepow1')
            pow_machine2 = self.total_('#activepow2')
            pow_machine3 = self.total_('#activepow3')
            print(f"consumption from machine 1: {pow_machine1} joules, consumption from machine 2: {pow_machine2} joules, consumption from machine 3: {pow_machine3} joules")
        else:
            print()
            print("PowerMeter recordings not available")
