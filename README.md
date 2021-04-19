# nrt-predict

Run prediction models on [Digital Earth Australia](https://www.ga.gov.au/dea) Near-Real-Time (NRT) satellite observations. The NRT data is an effort to acquire, atmospherically-correct, and package the data as quickly as possible from when a [Sentinel-2](http://www.esa.int/Applications/Observing_the_Earth/Copernicus/Sentinel-2) satellite flies over an area in Australia. **nrt-predict** provides a customisable workflow framework for running various machine learning, AI, and statistical models on the data to produce additional outputs. The aim is for the (user-defined) models to be both easy to write (minimal boilerplate) but also customisable (if needed). Ancillary datasets can be used by the models and **nrt-predict** retrieves and crops these datasets automatically for the model.

## Information

### NRT Data

The Digital Earth Australia NRT data can be found in Amazon s3 buckets of the form:
```
s3://dea-public-data/L2/sentinel-2-nrt/S2MSIARD/<date>/<package>
```
The data can also be accessed through HTTPS at:
```
https://data.dea.ga.gov.au/L2/sentinel-2-nrt/S2MSIARD/<date>/<package>
```

A (minified!) version of a package layout can be found in the [data/test](https://github.com/daleroberts/nrt-predict/tree/main/data/test/) directory of this repo.
