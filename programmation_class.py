import defaults
import uuid
from cronsim import CronSim
from datetime import datetime, timedelta
from mergedeep import merge
import config_manager

__cm = config_manager.ConfigManager()
programmation_interval = __cm.get_app_params().get('_programmation_interval')

class Programmation:
    def __init__(self, programmation=None, source_programmation=defaults.programmation_object_default, id=None, *args, **kwargs):
        merged_programmation = merge({}, source_programmation, programmation)
        merged_planning = merged_programmation.get('planning')

        if id is None:
            self.id = f'{uuid.uuid4()}'
        else:
            self.id = id

        self.url = merged_programmation.get('url')
        self.user_token = merged_programmation.get('user_token')
        self.enabled = merged_programmation.get('enabled')
        self.presets = merged_programmation.get('presets')

        self.recording_start_date = merged_planning.get('recording_start_date')
        self.recording_duration = merged_planning.get('recording_duration')
        self.recording_stops_at_end = merged_planning.get('recording_stops_at_end')
        self.recording_restarts_during_duration = merged_planning.get('recording_restarts_during_duration')
        self.recurrence_cron = merged_planning.get('recurrence_cron')
        self.recurrence_start_date = merged_planning.get('recurrence_start_date')
        self.recurrence_end_date = merged_planning.get('recurrence_end_date')

        self.planning = {
            "recording_start_date": self.recording_start_date,
            "recording_duration": self.recording_duration,
            "recording_stops_at_end": self.recording_stops_at_end,
            "recording_restarts_during_duration": self.recording_restarts_during_duration,
            "recurrence_cron": self.recurrence_cron,
            "recurrence_start_date": self.recurrence_start_date,
            "recurrence_end_date": self.recurrence_end_date,
        }

        self.errors = self.validate_programmation()

        if len(self.errors) == 0:
            self.recording_start_date = datetime.fromisoformat(
                self.recording_start_date) if self.recording_start_date is not None else None
            self.recurrence_start_date = datetime.fromisoformat(
                self.recurrence_start_date) if self.recurrence_start_date is not None else None
            self.recurrence_end_date = datetime.fromisoformat(
                self.recurrence_end_date) if self.recurrence_end_date is not None else None

    def get(self, censored=False):
        return {
            "id": self.id,
            "url": self.url,
            "user_token": self.user_token if not censored else '',
            "enabled": self.enabled,
            "presets": self.presets,
            "planning": self.planning,
        }

    def validate_programmation(self, *args, **kwargs):
        errors_list = []

        date_controls = ['recurrence_start_date', 'recurrence_end_date', 'recording_start_date']

        for field in date_controls:
            if self.planning.get(field) is not None:
                try:
                    datetime.fromisoformat(self.planning.get(field))
                except Exception as error:
                    errors_list.append({'field': field, 'value': self.planning.get(field), 'error': f'{error}'})

        if self.url is None or self.url == '':
            errors_list.append({'field': 'url', 'error': 'url is empty'})

        if type(self.recording_stops_at_end) != bool:
            errors_list.append({'field': 'recording_stops_at_end', 'error': 'must be a boolean'})

        if type(self.recording_restarts_during_duration) != bool:
            errors_list.append({'field': 'recording_restarts_during_duration', 'error': 'must be a boolean'})

        if type(self.enabled) != bool:
            errors_list.append({'field': 'enabled', 'error': 'must be a boolean'})

        if self.presets is not None and type(self.presets) != list:
            errors_list.append({'field': 'presets', 'error': 'must be None or list'})

        if self.recording_duration is not None:
            if type(self.recording_duration) != int:
                errors_list.append({'field': 'recording_duration', 'error': 'must be None or int'})
            if type(self.recording_duration) == int and self.recording_duration < 1:
                errors_list.append({'field': 'recording_duration',
                                    'value': self.recording_duration,
                                    'error': 'must be greater than 0'})

        if self.recurrence_cron is not None and self.recording_start_date is not None:
            errors_list.append({'field': 'recurrence_cron', 'error': 'cannot be use with recording_start_date field'})
            errors_list.append({'field': 'recording_start_date', 'error': 'cannot be use with recurrence_cron field'})

        try:
            if self.recurrence_cron is not None:
                # Just to validate it's a correct cron format
                CronSim(self.recurrence_cron, datetime.now())

        except Exception as e:
            errors_list.append({'field': 'recurrence_cron',
                                'value': self.recurrence_cron,
                                'error': f'{e}'})

        return errors_list

    def get_end_date(self, *args, **kwargs):
        end_date = None

        if self.planning is None:
            return None

        if self.recurrence_end_date is None and self.recording_start_date is not None and self.recording_duration is not None:
            end_date = self.recording_start_date + timedelta(minutes=self.recording_duration)

        elif self.recurrence_end_date is None and self.recording_start_date is not None:
            end_date = self.recording_start_date

        elif self.recurrence_end_date is not None and self.recording_duration is not None:
            end_date = self.recurrence_end_date + timedelta(minutes=self.recording_duration)

        elif self.recurrence_end_date is not None:
            end_date = self.recurrence_end_date

        return end_date

    def must_be_restarted(self, from_date=None, *args, **kwargs):
        from_date = datetime.now() if from_date is None else from_date

        if self.recording_duration is None or self.recording_restarts_during_duration is False:
            return None
        elif self.recording_start_date is not None:
            start_date = self.recording_start_date
        elif self.recurrence_start_date is not None and self.recording_duration is not None:
            start_date = next(CronSim(self.recurrence_cron, from_date - timedelta(minutes=self.recording_duration)))
        else:
            return None

        if start_date < from_date < start_date + timedelta(minutes=self.recording_duration):
            return int((start_date + timedelta(minutes=self.recording_duration) - from_date).seconds / 60)
        else:
            return None

    def get_next_execution(self, from_date=None, *args, **kwargs):
        from_date = datetime.now() if from_date is None else from_date

        if self.recording_start_date is not None:
            return self.recording_start_date

        if self.recurrence_cron is not None:
            if self.recurrence_start_date is not None and self.recurrence_start_date > from_date:
                start_date = self.recurrence_start_date
            else:
                start_date = from_date

            return next(CronSim(self.recurrence_cron, start_date - timedelta(seconds=programmation_interval)))

        return None

    def set_id(self, id=None):
        self.id = id

    def __str__(self):
        return f'{self.get()}'
