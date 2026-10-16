### **BETA TEST PLAN**

## **1. Project context**
HuRI is a research-driven platform that has the objective to act as a universal middleware for speech and movement for any various physical or digital human embodiments. The focus on our part is to have embodiments as human as possible, regarding the emotion aspect, and try to go beyond the uncanny valley. The goal is to create a modular ecosystem where any combination of AI can control any robot or any virtual avatar. The project focuses on a highly stable architecture, scalable cloud deployment, and innovative emotional expression through motion and voice.

## **2. User role**

| **Role Name** | **Description** |
|---------------|-----------------|
| Module Developer | Uses the library to build and package custom plugins or specialized modules for specific use cases. |
| DevOps Engineer | Handles the containerization, orchestration, and distribution of the library across different network nodes or edge devices. |
| Client | Integrates the library into their own projects without concern for its internal implementation. |


## **3. Feature table**

| **Feature ID** | **User role** | **Feature name** | **Short description** |
|--------------|---------------|-------------------------|--------------------------------------|
| F1 | Module Developer | Create a Module | Build and integrate modules into the library. |
| F2 | Module Developer & DevOps Engineer | Configure HuRI | Define available modules and deployement settings for HuRI. |
| F3 | Client | Configure Modules | Define and load different module combinations through a configuration file. |
| F4 | Everyone | Run in Parallel | Distribute computation across 1 to N machines to balance the payload. |
| F5 | Everyone | Handle Multi-client | Handle 1 to N simultaneous client instances with separated discussions. |
| F6 | Everyone | Detect Voice Activity | Detect when the user is speaking. |
| F7 | Everyone | Transcribe Speech |  Generate text from an audio speech. |
| F8 | Everyone | Generate Speech | Generate speech audio from a given text input. |
| F9 | Everyone | Generate Body Movement | Generate body movements by placing points in space. |
| F10 | Everyone | Retrieve and Augment | Retrieve text from files, saved texts, or past conversations and generate an output. |
| F11 | Everyone | Recognise Speech Emotion | Recognise emotions from user speech. |
| F12 | Everyone | Link Emotion to Input | Link a transcript with its associated emotion. |
| F13 | Everyone | Manage Artificial Memory | Store and manage an artificial memory for HuRI. |
| F14 | Everyone | Send User Audio | Transmit user audio to the HuRI system. |
| F15 | Everyone | Send User Text | Transmit user text to the HuRI system. |
| F16 | Everyone | Receive Generated Audio | Deliver generated audio back to the user. |
| F17 | Everyone | Receive Generated Text | Deliver generated text back to the user. |
| F18 | Everyone | Store Vectorised Data | Store and retrieve vectorised data through a dedicated service. |
| F19 | Everyone | Call LLM Service | Generate a response by calling an external LLM service. |

---

## **4. Success Criteria**

| **Feature ID** | **Key success criteria** | **Indicator/metric** | **Result** |
|--------------|---------------------------------------|-----------------------|----------------|
| F1 | Verify modules can be written and run inside HuRI. | 20 attempts -- expected 100% | Achieved (/20) |
| F2 | Check that available modules and cluster settings correspond to the configuration. | 5 configurations -- expected 100% | Achieved (/5) |
| F3 | Confirm that a specific module combination loads from a config file. | 5 files -- expected 100% | Achieved (/5) |
| F4 | Test that HuRI runs across multiple machines with computation distributed. | 1 machine / N machines | Scenario achieved (/2) |
| F5 | Verify multiple clients stay isolated with separate discussions. | 3 simultaneous instances -- expected 100% isolation | Instances isolated (/3) |
| F6 | Check that speaking and silence are correctly detected. | 10 voice samples -- expected 100% | Detections successful (/10) |
| F7 | Confirm spoken input is transcribed accurately. | 20 spoken phrases -- expected 80% accuracy | Phrases correct (/20) |
| F8 | Verify a speech audio file with human characteristic is generated from a text input accurately. | 10 text inputs -- expected 100% generation -- expected 60% human feeling* | Files generated (/10) -- Human feedback in % |
| F9 | Assess whether body movements feel human-like. | 10 movement sequences -- expected 60% human feeling | Human feedback in % |
| F10 | Check that relevant saved content is retrieved to build an answer. | 10 queries on saved text -- expected 100% retrieval | Relevant text found (/10) |
| F11 | Verify the emotion in user speech is correctly identified. | 10 emotion samples -- expected 60% accuracy | Emotions recognised (/10) |
| F12 | Confirm the detected emotion is associated with the transcript. | 10 emotional contexts -- expected 60% | Contexts linked (/10) |
| F13 | Test that information is retained, updated, and forgotten over time. | 10 pieces of info over time -- expected 60% | Prompts saved and treated (/10) |
| F14 | Verify user audio is transmitted correctly to the HuRI system. | 10 audio inputs -- expected 100% | Inputs received (/10) |
| F15 | Confirm user text is transmitted correctly to the HuRI system. | 10 text inputs -- expected 100% | Inputs received (/10) |
| F16 | Check that generated audio is correctly delivered back to the user. | 10 audio files -- expected 100% | Files heard (/10) |
| F17 | Confirm generated text is correctly delivered back to the user. | 10 text outputs -- expected 100% | Outputs displayed (/10) |
| F18 | Verify vectorised data is stored and retrieved accurately. | 10 operations -- expected 100% | Operations successful (/10) |
| F19 | Test that a coherent response is returned from the external LLM service. | 10 prompts -- expected 100% generation | Answers generated (/10) |


*Experiments will be conducted on several people and a Godspeed-based questionnaire will evaluate the human feeling