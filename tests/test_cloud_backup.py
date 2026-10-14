import importlib.util
import sqlite3
import tempfile
import unittest
import zipfile
import os
from contextlib import closing
from pathlib import Path
from workdesk.store import Store

def tool(name):
    path=Path(__file__).resolve().parents[1]/'tools'/(name+'.py')
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

class BackupTests(unittest.TestCase):
    def test_migration_and_backup_preserve_canonical_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=Store(root/'desktop');obj=store.add_object('模拟群')
            md=root/'chat.md';md.write_text('仅为模拟记录','utf8');store.register_import(obj['id'],md)
            destination=root/'migrated'
            info=tool('prepare_cloud_data').prepare(store.root,destination,str(destination))
            self.assertEqual(info['source_count'],1)
            migrated=Store(destination)
            with migrated.connect() as con:sid=con.execute('SELECT id FROM sources').fetchone()[0]
            self.assertEqual(migrated.source(sid)[1].read_text('utf8'),'仅为模拟记录')
            archive=tool('backup_cloud').backup(destination,root/'backups')
            with zipfile.ZipFile(archive) as z:
                self.assertIsNone(z.testzip());self.assertIn('workdesk.sqlite3',z.namelist())
                z.extract('workdesk.sqlite3',root/'restore')
            with closing(sqlite3.connect(root/'restore/workdesk.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT id FROM objects').fetchone()[0],obj['id'])
            with self.assertRaises(ValueError):tool('prepare_cloud_data').prepare(store.root,destination)

    @unittest.skipUnless(os.name=='nt','长中文原文件名来自 Windows')
    def test_long_unicode_snapshot_filename_maps_to_portable_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=Store(root/'desktop');obj=store.add_object('模拟群')
            md=root/(('很长的中文附件名'*12)+'.md');md.write_text('模拟长文件名','utf8');store.register_import(obj['id'],md)
            destination=root/'migrated';tool('prepare_cloud_data').prepare(store.root,destination,str(destination))
            migrated=Store(destination)
            with migrated.connect() as con:row=dict(con.execute('SELECT * FROM sources').fetchone())
            self.assertEqual(row['relative_path'],md.name)
            self.assertEqual(Path(row['snapshot_path']).name,'snapshot.md')
            self.assertEqual(migrated.source(row['id'])[1].read_text('utf8'),'模拟长文件名')

if __name__=='__main__':unittest.main()
