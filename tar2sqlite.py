"""
Extracts a LEGI tar archive into an SQLite DB
"""

from __future__ import division, print_function, unicode_literals

from argparse import ArgumentParser
import fnmatch
import os
import re
from sqlite3 import OperationalError

import libarchive
from lxml import etree

from utils import connect_db, reconstruct_path


def innerHTML(e):
    i = len(e.tag) + 2
    return etree.tostring(e, encoding='unicode', with_tail=False)[i:-i-1]


def scrape_tags(attrs, root, wanted_tags, unwrap=False):
    attrs.update(
        (e.tag.lower(), (innerHTML(e[0]) if unwrap else innerHTML(e)) or None)
        for e in root if e.tag in wanted_tags
    )


def make_schema(db):
    with open('schema.sql', 'r') as f:
        db.executescript(f.read())


def suppress(get_table, db, liste_suppression):
    deleted = 0
    for path in liste_suppression:
        parts = path.split('/')
        assert parts[0] == 'legi'
        text_cid = parts[11]
        text_id = parts[-1]
        assert len(text_id) == 20
        table = get_table(parts)
        db.run("""
            DELETE FROM {0}
             WHERE dossier = ?
               AND cid = ?
               AND id = ?
        """.format(table), (parts[3], text_cid, text_id))
        changes = db.changes()
        if changes:
            deleted += changes
            if table in ('articles', 'textes_versions'):
                db.run("""
                    DELETE FROM liens
                     WHERE src_id = ? AND NOT _reversed
                        OR dst_id = ? AND _reversed
                """, (text_id, text_id))
                deleted += db.changes()
            elif table == 'sections':
                db.run("""
                    DELETE FROM sommaires
                     WHERE cid = ?
                       AND parent = ?
                       AND _source = 'section_ta_liens'
                """, (text_cid, text_id))
                deleted += db.changes()
            elif table == 'textes_structs':
                db.run("""
                    DELETE FROM sommaires
                     WHERE cid = ?
                       AND _source = 'struct/' || ?
                """, (text_cid, text_id))
                deleted += db.changes()
    print('deleted', deleted, 'rows based on liste_suppression_legi.dat')


def process_archive(db, archive_path, old_files_log):

    # Define some constants
    ARTICLE_TAGS = set('NOTA BLOC_TEXTUEL'.split())
    SECTION_TA_TAGS = set('TITRE_TA COMMENTAIRE'.split())
    TEXTELR_TAGS = set('VERSIONS'.split())
    TEXTE_VERSION_TAGS = set('VISAS SIGNATAIRES TP NOTA ABRO RECT'.split())
    META_ARTICLE_TAGS = set('NUM ETAT DATE_DEBUT DATE_FIN TYPE'.split())
    META_CHRONICLE_TAGS = set("""
        NUM NUM_SEQUENCE NOR DATE_PUBLI DATE_TEXTE DERNIERE_MODIFICATION
        ORIGINE_PUBLI PAGE_DEB_PUBLI PAGE_FIN_PUBLI
    """.split())
    META_VERSION_TAGS = set(
        'TITRE TITREFULL ETAT DATE_DEBUT DATE_FIN AUTORITE MINISTERE'.split()
    )
    SOUS_DOSSIER_MAP = {
        'articles': 'article',
        'sections': 'section_ta',
        'textes_structs': 'texte/struct',
        'textes_versions': 'texte/version',
    }
    TABLES_MAP = {'ARTI': 'articles', 'SCTA': 'sections', 'TEXT': 'textes_'}
    TYPELIEN_MAP = {
        "ABROGATION": "ABROGE",
        "ANNULATION": "ANNULE",
        "CODIFICATION": "CODIFIE",
        "CONCORDANCE": "CONCORDE",
        "CREATION": "CREE",
        "DEPLACE": "DEPLACEMENT",
        "DISJOINT": "DISJONCTION",
        "MODIFICATION": "MODIFIE",
        "PEREMPTION": "PERIME",
        "RATIFICATION": "RATIFIE",
        "TRANSFERE": "TRANSFERT",
    }
    TYPELIEN_MAP.update([(v, k) for k, v in TYPELIEN_MAP.items()])

    # Define some shortcuts
    attr = etree._Element.get
    insert = db.insert
    update = db.update

    def get_table(parts):
        table = TABLES_MAP[parts[-1][4:8]]
        if table == 'textes_':
            table += parts[13] + 's'
        return table

    old_files_count = 0
    liste_suppression = []
    xml = etree.XMLParser(remove_blank_text=True)
    with libarchive.file_reader(archive_path) as archive:
        for entry in archive:
            path = entry.pathname
            if path[-1] == '/':
                continue
            parts = path.split('/')
            if parts[-1] == 'liste_suppression_legi.dat':
                liste_suppression += b''.join(entry.get_blocks()).decode('ascii').split()
                continue
            if parts[1] == 'legi':
                path = path[len(parts[0])+1:]
                parts = parts[1:]
            dossier = parts[3]
            text_cid = parts[11]
            text_id = parts[-1][:-4]
            mtime = entry.mtime

            table = get_table(parts)
            prev_row = db.one("""
                SELECT mtime, dossier, cid
                  FROM {0}
                 WHERE id = ?
            """.format(table), (text_id,))
            if prev_row:
                prev_mtime, prev_dossier, prev_cid = prev_row
                if prev_mtime == mtime:
                    continue
                if prev_dossier != dossier or prev_cid != text_cid:
                    old_files_count += 1
                    if prev_mtime > mtime:
                        print(path, file=old_files_log)
                        continue
                    else:
                        prev_path = reconstruct_path(
                            prev_dossier,
                            prev_cid,
                            SOUS_DOSSIER_MAP[table],
                            text_id,
                        )
                        print(prev_path, file=old_files_log)

            for block in entry.get_blocks():
                xml.feed(block)
            root = xml.close()
            tag = root.tag
            meta = root.find('META')

            # Check the ID
            if tag == 'SECTION_TA':
                assert root.find('ID').text == text_id
            else:
                meta_commun = meta.find('META_COMMUN')
                assert meta_commun.find('ID').text == text_id
                nature = meta_commun.find('NATURE').text

            # Extract the data we want
            attrs = {}
            if tag == 'ARTICLE':
                assert nature == 'Article'
                assert table == 'articles'
                contexte = root.find('CONTEXTE/TEXTE')
                assert attr(contexte, 'cid') == text_cid
                sections = contexte.findall('.//TITRE_TM')
                if sections:
                    attrs['section'] = attr(sections[-1], 'id')
                meta_article = meta.find('META_SPEC/META_ARTICLE')
                scrape_tags(attrs, meta_article, META_ARTICLE_TAGS)
                scrape_tags(attrs, root, ARTICLE_TAGS, unwrap=True)
            elif tag == 'SECTION_TA':
                assert table == 'sections'
                scrape_tags(attrs, root, SECTION_TA_TAGS)
                section_id = text_id
                contexte = root.find('CONTEXTE/TEXTE')
                assert attr(contexte, 'cid') == text_cid
                parents = contexte.findall('.//TITRE_TM')
                if parents:
                    attrs['parent'] = attr(parents[-1], 'id')
                if prev_row:
                    db.run("""
                        DELETE FROM sommaires
                         WHERE cid = ?
                           AND parent = ?
                           AND _source = 'section_ta_liens'
                    """, (text_cid, section_id))
                for i, lien in enumerate(root.find('STRUCTURE_TA')):
                    insert('sommaires', {
                        'cid': text_cid,
                        'parent': section_id,
                        'element': attr(lien, 'id'),
                        'debut': attr(lien, 'debut'),
                        'fin': attr(lien, 'fin'),
                        'etat': attr(lien, 'etat'),
                        'num': attr(lien, 'num'),
                        'position': i,
                        '_source': 'section_ta_liens',
                    })
            elif tag == 'TEXTELR':
                assert table == 'textes_structs'
                scrape_tags(attrs, root, TEXTELR_TAGS)
                source = 'struct/' + text_id
                if prev_row:
                    db.run("""
                        DELETE FROM sommaires
                         WHERE cid = ?
                           AND _source = ?
                    """, (text_cid, source))
                for i, lien in enumerate(root.find('STRUCT')):
                    insert('sommaires', {
                        'cid': text_cid,
                        'element': attr(lien, 'id'),
                        'debut': attr(lien, 'debut'),
                        'fin': attr(lien, 'fin'),
                        'etat': attr(lien, 'etat'),
                        'position': i,
                        '_source': source,
                    })
            elif tag == 'TEXTE_VERSION':
                assert table == 'textes_versions'
                attrs['nature'] = nature
                meta_spec = meta.find('META_SPEC')
                meta_chronicle = meta_spec.find('META_TEXTE_CHRONICLE')
                assert meta_chronicle.find('CID').text == text_cid
                scrape_tags(attrs, meta_chronicle, META_CHRONICLE_TAGS)
                meta_version = meta_spec.find('META_TEXTE_VERSION')
                scrape_tags(attrs, meta_version, META_VERSION_TAGS)
                scrape_tags(attrs, root, TEXTE_VERSION_TAGS, unwrap=True)
            else:
                raise Exception('unexpected tag: '+tag)

            if tag in ('ARTICLE', 'TEXTE_VERSION'):
                if prev_row:
                    db.run("""
                        DELETE FROM liens
                         WHERE src_id = ? AND NOT _reversed
                            OR dst_id = ? AND _reversed
                    """, (text_id, text_id))
                e = root if tag == 'ARTICLE' else meta_version
                liens = e.find('LIENS')
                if liens is not None:
                    for lien in liens:
                        typelien, sens = attr(lien, 'typelien'), attr(lien, 'sens')
                        src_id, dst_id = text_id, attr(lien, 'id')
                        if sens == 'cible':
                            assert dst_id
                            src_id, dst_id = dst_id, src_id
                            dst_cid = dst_titre = ''
                            typelien = TYPELIEN_MAP.get(typelien, typelien+'_R')
                            _reversed = True
                        else:
                            dst_cid = attr(lien, 'cidtexte')
                            dst_titre = lien.text
                            _reversed = False
                        insert('liens', {
                            'src_id': src_id,
                            'dst_cid': dst_cid,
                            'dst_id': dst_id,
                            'dst_titre': dst_titre,
                            'typelien': typelien,
                            '_reversed': _reversed,
                        })

            attrs['dossier'] = dossier
            attrs['cid'] = text_cid
            attrs['mtime'] = mtime

            if prev_row:
                update(table, dict(id=text_id), attrs)
            else:
                attrs['id'] = text_id
                insert(table, attrs)

    print('detected', old_files_count, 'old files, logged into', old_files_log.name)

    if liste_suppression:
        suppress(get_table, db, liste_suppression)


def main():
    p = ArgumentParser()
    p.add_argument('db')
    p.add_argument('directory')
    args = p.parse_args()

    old_files_log = open(args.db+'.old_files.dat', 'a')
    db = connect_db(args.db)

    # Create the DB schema if necessary
    try:
        db.run("SELECT 1 FROM textes_versions LIMIT 1")
    except OperationalError:
        make_schema(db)

    # Look for new archives in the given directory
    last_update = db.one("SELECT value FROM db_meta WHERE key = 'last_update'")
    print("> last_update is", last_update)
    archive_re = re.compile(r'(.+_)?legi(?P<global>_global)?_(?P<date>[0-9]{8}-[0-9]{6})\..+')
    skipped = 0
    files = sorted(os.listdir(args.directory))
    for archive_name in fnmatch.filter(files, '*legi_*.tar.*'):
        m = archive_re.match(archive_name)
        if not m:
            print("unable to extract date from archive filename", archive_name)
            continue
        is_delta = not m.group('global')
        if bool(last_update) ^ is_delta:
            skipped += 1
            continue
        archive_date = m.group('date')
        if last_update and archive_date <= last_update:
            skipped += 1
            continue

        # Okay, process this one
        if skipped:
            print("> Skipped %i old archives" % skipped)
            skipped = 0
        print("> Processing %s..." % archive_name)
        with db:
            process_archive(db, args.directory + '/' + archive_name, old_files_log)
            if last_update:
                db.run("UPDATE db_meta SET value = ? WHERE key = 'last_update'", (archive_date,))
            else:
                db.run("INSERT INTO db_meta VALUES ('last_update', ?)", (archive_date,))
            last_update = archive_date
            print('last_update is now set to', last_update)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
