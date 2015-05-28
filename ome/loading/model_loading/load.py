# -*- coding: utf-8 -*-

from ome import base, settings, components, timing
from ome.models import Model
from ome.loading import AlreadyLoadedError
from ome.loading.model_loading import loading_methods, parse
from ome.dumping.model_dumping import dump_model

import cobra.io
import os
from os.path import join, basename, abspath, dirname 
import logging
import shutil
import subprocess

def get_model_list():
    """Get the models that are available, as SBML, in ome_data/models"""
    return [x.replace('.xml', '').replace('.mat', '') for x in
            os.listdir(join(settings.data_directory, 'models'))
            if '.xml' in x or '.mat' in x]

def check_for_model(name):
    """Check for model, case insensitive, and ignore periods and underscores"""
    def min_name(n):
        return n.lower().replace('.','').replace(' ','').replace('_','')
    for x in get_model_list():
        if min_name(name)==min_name(x):
            return x
    return None

@timing
def load_model(model_filepath, bioproject_id, model_timestamp, pmid, session,
               dump_directory=settings.model_dump_directory,
               published_directory=settings.model_published_directory):
    """Load a model into the database. Returns the bigg_id for the new model.

    Arguments
    ---------

    model_filepath: the path to the file where model is stored.

    bioproject_id: id for the loaded genome annotation.

    model_timestamp: a timestamp for the model.

    pmid: a publication PMID for the model.

    """

    # apply id normalization
    logging.debug('Parsing SBML')
    model, old_parsed_ids = parse.load_and_normalize(model_filepath)
    model_bigg_id = model.id
    
    # check that the model doesn't already exist
    if session.query(Model).filter_by(bigg_id=model_bigg_id).count() > 0:
        raise AlreadyLoadedError('Model %s already loaded' % model_bigg_id)

    # check for a genome annotation for this model
    if bioproject_id is not None:
        genome_db = session.query(base.Genome).filter_by(bioproject_id=bioproject_id).first()
        if bioproject_id == 'PRJNA224116':
            logging.warn('THIS IS A TERRIBLE SOLUTION. SEE https://github.com/SBRG/BIGG2/issues/68')
            if model_bigg_id == 'iHN637':
                organism = 'Clostridium ljungdahlii DSM 13528'
            elif model_bigg_id == 'iSB619':
                organism = 'Staphylococcus aureus subsp. aureus N315'
            else:
                raise Exception('My terrible fix broke for model {}'.format(model_bigg_id))
            genome_db = (session
                        .query(base.Genome)
                        .filter(base.Genome.bioproject_id == bioproject_id)
                        .filter(base.Genome.organism == organism)
                        .first())
        if genome_db is None:
            raise Exception('Genbank file %s for model %s not found in the database' %
                            (bioproject_id, model_bigg_id))
        genome_id = genome_db.id
    else:
        logging.info('No BioProject ID provided for model {}'.format(model_bigg_id))
        genome_id = None

    # Load the model objects. Remember: ORDER MATTERS! So don't mess around.
    logging.debug('Loading objects for model {}'.format(model.id))
    published_filename = os.path.basename(model_filepath)
    model_database_id = loading_methods.load_model(session, model, genome_id,
                                                   model_timestamp, pmid,
                                                   published_filename)

    # metabolites/components and linkouts
    # get compartment names
    if os.path.exists(settings.compartment_names):
        with open(settings.compartment_names, 'r') as f:
            compartment_names = {}
            for line in f.readlines():
                sp = [x.strip() for x in line.split('\t')]
                try:
                    compartment_names[sp[0]] = sp[1]
                except IndexError:
                    continue
    else:
        logging.warn('No compartment names file')
        compartment_names = {}
    loading_methods.load_metabolites(session, model_database_id, model,
                                     compartment_names,
                                     old_parsed_ids['metabolites'])

    # reactions
    model_db_rxn_ids = loading_methods.load_reactions(session, model_database_id,
                                                      model, old_parsed_ids['reactions'])

    # genes
    loading_methods.load_genes(session, model_database_id, model,
                               model_db_rxn_ids)

    # count model objects for the model summary web page
    loading_methods.load_model_count(session, model_database_id)

    session.commit()
    
    if dump_directory:
        # dump database models
        logging.info('Dumping {}'.format(basename(model_bigg_id)))
        cobra_model = dump_model(model_bigg_id)
        # make folder if it doesn't exist
        try:
            os.makedirs(dump_directory)
        except OSError:
            pass
        cobra.io.write_sbml_model(cobra_model, join(dump_directory, model_bigg_id + '.xml'))
        cobra.io.save_json_model(cobra_model, join(dump_directory, model_bigg_id + '.json'))

    if published_directory:
        # make folder if it doesn't exist
        try:
            os.makedirs(published_directory)
        except OSError:
            pass
        # copy published model
        try:
            logging.info('Copying {} to static directory'
                         .format(basename(model_filepath)))
            shutil.copy(model_filepath, published_directory)
        except OSError:
            print('Could not copy published model {}'.format(model_filepath))
    
    return model_bigg_id


def run_model_polisher(dump_directory, polished_directory):
    try:
        os.makedirs(polished_directory)
    except OSError:
        pass

    model_polisher_path = abspath(join(dirname(__file__), '..', '..', '..',
                                       'bin', 'ModelPolisher-0.2.jar'))
    logging.info('Running model polisher with {}'.format(model_polisher_path))

    command = [settings.java,
               '-jar',
               '-Xms8G',
               '-Xmx8G',
               '-Duser.language=en',
               model_polisher_path,
               '--user=%s' % settings.postgres_user,
               '--host=%s' % settings.postgres_host,
               '--dbname=%s' % settings.postgres_database,
               '--input=%s' % dump_directory,
               '--output=%s' % polished_directory,
               '--compress-output=true',
               '--omit-generic-terms=false',
               '--log-level=INFO',
               '--log-file=model_polisher.log']
    print ' '.join(command)
    subprocess.call(command)
