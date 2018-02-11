from bson import json_util
from pyspark import StorageLevel

from bicis.lib.utils import get_logger
logger = get_logger(__name__)

import json
import os

import luigi
from luigi.contrib.spark import PySparkTask
from pyspark.sql import SparkSession

from bicis.etl.unify_raw_data import UnifyRawData
from bicis.lib.data_paths import data_dir
from bicis.lib.object_loader import ObjectLoader


class BuildFeaturesDataset(PySparkTask):
    features_config_fname = luigi.Parameter()

    @property
    def object_loader(self):
        return ObjectLoader.from_yaml(self.features_config_fname)

    @property
    def feature_builder(self):
        return self.object_loader.get('features_builder')

    def requires(self):
        return {
            # Each feature builder depends on different datasets,
            # thus this requires is delegated to the specified feature builder
            'builder_requirements': self.feature_builder.requires(),
            'raw_data': UnifyRawData()
        }

    def output(self):
        fname = os.path.basename(self.features_config_fname.replace('.yaml', ''))
        fname_prefix = os.path.join(data_dir, fname)
        return {
            'features': luigi.LocalTarget(fname_prefix + '.csv'),
            'fails': luigi.LocalTarget(fname_prefix + '.fails')
        }

    def main(self, sc, *args):
        logger.info('Starting building features')

        spark_sql = SparkSession.builder.getOrCreate()

        input_df = (
            spark_sql
                .read
                .load(
                    self.input()['raw_data'].path,
                    format="csv",
                    sep=",",
                    inferSchema="true",
                    header="true"
            )
        )

        features_rdd = (
            input_df
            # There are some null rows
            # TODO: check whether this is a parsing error
            .filter(input_df.rent_station.isNotNull())
            .rdd
            .map(lambda x: (x['id'], self.feature_builder.get_features(x['rent_station'], x['rent_date'])))
            .persist(StorageLevel.DISK_ONLY)
        )

        output = (
            features_rdd
            # Filter out the ones that failed
            .filter(lambda x: x[1] is not None)
            .map(lambda x: x[1])
            .toDF()
        )

        if not self.output()['features'].exists():
            logger.info('Saving dataset')

            output.write.csv(self.output()['features'].path, header='true')

        if not self.output()['fails'].exists():
            logger.info('Collecting some fails')

            output_count = output.count()
            input_count = input_df.count()
            error_ids = (
                features_rdd
                .filter(lambda x:x[1] is None)
                .map(lambda x: x[0])
                .take(100)
            )

            with self.output()['fails'].open('w') as f:
                json.dump(
                    {
                        'input_count': input_count,
                        'output_count': output_count,
                        'number_of_errors': input_count - output_count,
                        'error_ids': error_ids
                    },
                    f, indent=2,
                    # used to make datetime serializable
                    default=json_util.default
                )

        logger.info('Done')

