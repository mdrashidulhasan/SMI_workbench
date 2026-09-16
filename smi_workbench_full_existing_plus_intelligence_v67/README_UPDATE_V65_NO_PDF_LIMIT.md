# v65 no separate PDF limit

This update removes the PDF-specific limit from the New Run UI and generated workflow config.

The PDF download stage now attempts PDFs for every record already present in `04_retrieved_records.json`. There is no separate PDF limit field and no `SMI_PDF_LIMIT` environment variable. The only upstream size control is the record retrieval step itself.

PDF batching and worker settings remain for reliability/performance, but they do not limit the number of retrieved records considered for PDF download.
