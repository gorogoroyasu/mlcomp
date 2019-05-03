from mlcomp.db.providers.base import *


class DagStorageProvider(BaseDataProvider):
    model = DagStorage

    def by_dag(self, dag: int):
        query = self.query(DagStorage, File).join(File, isouter=True).filter(DagStorage.dag == dag). \
            order_by(DagStorage.path)
        return query.all()
