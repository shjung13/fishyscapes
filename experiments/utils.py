from pandas import Series
from pymongo import MongoClient
from gridfs import GridFS
from tensorflow.python.summary.summary_iterator import summary_iterator
import fs.settings as settings
from bson.json_util import dumps
import json
import zipfile
from os import path, listdir
from numpy import array, nan, inf
from copy import deepcopy


def reverse_convert_datatypes(data):
    if isinstance(data, dict):
        if 'values' in data and len(data) == 1:
            return reverse_convert_datatypes(data['values'])
        if 'py/tuple' in data and len(data) == 1:
            return reverse_convert_datatypes(data['py/tuple'])
        if 'py/object' in data and data['py/object'] == 'numpy.ndarray':
            if 'dtype' in data:
                return array(data['values'], dtype=data['dtype'])
            else:
                return array(data['values'])
        for key in data:
            data[key] = reverse_convert_datatypes(data[key])
        return data
    elif isinstance(data, list):
        return [reverse_convert_datatypes(item) for item in data]
    elif isinstance(data, str) and len(data) > 0 and data[0] == '[':
        return eval(data)
    return data


class ExperimentData:
    """Loads experimental data from experiments database."""

    def __init__(self, exp_id):
        """Load data for experiment with id 'exp_id'.

        Follwing the settings, data is either loaded from the mongodb connection or,
        as a fallback, from the specified directory.

        Args:
          exp_id: EITHER int specifying the experiment number
                  OR string with a path to an experiment directory or .zip
        """
        load_from_path = isinstance(exp_id, str)
        if load_from_path and not path.exists(exp_id):
            raise UserWarning('Specified experiment %s not found.' % exp_id)

        if not load_from_path and hasattr(settings, 'EXPERIMENT_DB_HOST') \
                and settings.EXPERIMENT_DB_HOST:
            client = MongoClient('mongodb://{user}:{pwd}@{host}/{db}'.format(
                host=settings.EXPERIMENT_DB_HOST, user=settings.EXPERIMENT_DB_USER,
                pwd=settings.EXPERIMENT_DB_PWD, db=settings.EXPERIMENT_DB_NAME))
            self.db = client[settings.EXPERIMENT_DB_NAME]
            self.fs = GridFS(self.db)
            self.record = self.db.runs.find_one({'_id': exp_id})
            self.artifacts = [artifact['name']
                              for artifact in self.record['artifacts']]
        elif load_from_path or (hasattr(settings, 'EXPERIMENT_STORAGE_FOLDER')
                                and settings.EXPERIMENT_STORAGE_FOLDER):
            def load_from_directory(exp_path):
                self.exp_path = exp_path
                with open(path.join(self.exp_path, 'run.json')) as run_json:
                    record = json.load(run_json)
                with open(path.join(self.exp_path, 'info.json')) as info_json:
                    record['info'] = json.load(info_json)
                with open(path.join(self.exp_path, 'config.json')) as config_json:
                    record['config'] = json.load(config_json)
                with open(path.join(self.exp_path, 'cout.txt')) as captured_out:
                    record['captured_out'] = captured_out.read()
                self.record = record
                self.artifacts = listdir(self.exp_path)

            def load_from_zip(exp_path):
                self.zipfile = exp_path
                archive = zipfile.ZipFile(self.zipfile)
                record = json.loads(archive.read('run.json').decode('utf8'))
                record['info'] = json.loads(archive.read('info.json').decode('utf8'))
                record['config'] = json.loads(archive.read('config.json').decode('utf8'))
                record['captured_out'] = archive.read('cout.txt')
                archive.close()
                self.record = record
                self.artifacts = archive.namelist()

            if load_from_path:
                if path.isdir(exp_id):
                    load_from_directory(exp_id)
                else:
                    load_from_zip(exp_id)
            elif str(exp_id) in listdir(settings.EXPERIMENT_STORAGE_FOLDER):
                load_from_directory(path.join(settings.EXPERIMENT_STORAGE_FOLDER, exp_id))
            elif '%s.zip' % exp_id in listdir(settings.EXPERIMENT_STORAGE_FOLDER):
                load_from_zip(
                    path.join(settings.EXPERIMENT_STORAGE_FOLDER, '%s.zip' % exp_id))
            else:
                raise UserWarning('Specified experiment %s not found.' % exp_id)

    def get_record(self):
        """Get sacred record for experiment."""
        return reverse_convert_datatypes(deepcopy(self.record))

    def get_artifact(self, name):
        """Return the produced outputfile with given name as file-like object."""
        if name not in self.artifacts:
            raise UserWarning('ERROR: Artifact {} not found'.format(name))

        if hasattr(self, 'fs'):
            artifact_id = next(artifact['file_id']
                               for artifact in self.record['artifacts']
                               if artifact['name'] == name)
            return self.fs.get(artifact_id)
        elif hasattr(self, 'exp_path'):
            return open(path.join(self.exp_path, name))
        else:
            archive = zipfile.ZipFile(self.zipfile)
            return archive.open(name)

    def get_summary(self, tag):
        """Return pd.Series of scalar summary value with given tag."""
        search = [artifact for artifact in self.artifacts if 'events' in artifact]
        if not len(search) > 0:
            raise UserWarning('ERROR: Could not find summary file')
        summary_file = search[0]
        tmp_file = '/tmp/summary'
        with open(tmp_file, 'wb') as f:
            f.write(self.get_artifact(summary_file).read())
        iterator = summary_iterator(tmp_file)

        # go through all the values and store them
        step = []
        value = []
        for event in iterator:
            for measurement in event.summary.value:
                if (measurement.tag == tag):
                    step.append(event.step)
                    value.append(measurement.simple_value)
        return Series(value, index=step)

    def get_weights(self):
        if not hasattr(self, 'fs') and not hasattr(self, 'exp_path'):
            raise UserWarning('cannot load weights out of zipfile, please extract first')
        filename = next(artifact for artifact in self.artifacts if 'weights' in artifact)
        if hasattr(self, 'exp_path'):
            # better return the path than an opened file
            return path.join(self.exp_path, filename)
        else:
            return self.get_artifact(filename)

    def dump(self, path):
        """Dump the entire record and it's artifacts as a zip archieve."""
        if not path.endswith('.zip'):
            path = path + '.zip'
        archive = zipfile.ZipFile(path, 'w')
        for artifact in self.record['artifacts']:
            archive.writestr(artifact['name'], self.fs.get(artifact['file_id']).read())
        # following the FileStorageObserver, we need to create different files for config,
        # output, info and the rest of the record
        record = deepcopy(self.record)
        archive.writestr('config.json', dumps(record['config']))
        archive.writestr('cout.txt', record['captured_out'])
        archive.writestr('info.json', dumps(record['info']))
        record.pop('config', None)
        record.pop('captured_out', None)
        record.pop('info', None)
        archive.writestr('run.json', dumps(record))
        archive.close()

    def update_record(self, changes):
        """Apply changes to the record."""
        # so far only implemented for database version
        assert hasattr(self.db)
        self.record.update(changes)
        self.db.runs.replace_one({'_id': self.record['_id']}, self.record)
