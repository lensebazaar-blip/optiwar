import click
import MySQLdb
import MySQLdb.cursors
from flask import current_app, g
from flask.cli import with_appcontext

def get_db():
    if 'db' not in g:
        g.db = MySQLdb.connect(
           host=current_app.config['MYSQL_HOST'],
           user=current_app.config['MYSQL_USER'],
           password=current_app.config['MYSQL_PASSWORD'],
           database=current_app.config['MYSQL_DB'],
           cursorclass=MySQLdb.cursors.DictCursor,
           charset="utf8mb4",
           use_unicode=True
        )
    return g.db

def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    with current_app.open_resource('schema.sql') as f:
       with db.cursor() as cursor:
           cursor.execute(f.read().decode('utf8'))
    db.commit()

@click.command('init-db')
@with_appcontext
def init_db_command():
    ''' Clear existing data and create new tables'''
    init_db()
    click.echo('Initialized the database')

def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
