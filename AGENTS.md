The raw data for the BPI 2017 challenge is in bpi_2017_data. There are two .xes files. One for the core process information, one for the offers data.

Additionally there is an image showing the rough task flow at ENVIRONMENT_FLOWCHART.png.

First we'll want to create the necessary synthetic data using SYNTHETIC_DATAGEN_SPEC.md as reference. Store the resulting data in a /data folder. Then we want to create tasks. These tasks should be in the Harbor format (https://harborframework.com/docs) (also available in harbor_framework_docs.md). The desired structure of the environment overall is specified in ENVIRONMENTS_SPEC.md. It is describe more as a traditional environment and so will need a bit of work to implement it in the desired Harbor format.

Always use uv for any package management and code running.

You can use the `OPENROUTER_KEY` for any LLM usage you need, particularly for any synthetic datagen work and the Client Simulation.