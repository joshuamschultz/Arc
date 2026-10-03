const DATASTORE_KIND = /(database|datastore|sql|postgres|mysql|sqlite|warehouse)/i

/** Whether a source or extension kind is a database, so it browses as tables. */
export const isDatastoreKind = (kind: string) => DATASTORE_KIND.test(kind)
