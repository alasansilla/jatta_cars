from sqlalchemy import text
VERSION = '0007'
NAME = 'verified completed booking reviews'
def upgrade(connection, metadata):
    metadata.tables['booking_reviews'].create(connection, checkfirst=True)
    if connection.dialect.name == 'postgresql':
        connection.execute(text('ALTER TABLE booking_reviews ENABLE ROW LEVEL SECURITY'))
        connection.execute(text('REVOKE ALL ON booking_reviews FROM anon, authenticated'))
