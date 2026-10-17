# MD57 Capstone Defense: Slide-by-Slide Speaking Guide

Based on CAPS-FINAL1.pptx. Prepared 17 October 2026.

Presenter notes are preparation guidance and should not be read aloud. The original PPTX is unchanged.


## Slide 1: Title and Introduction

Good day, respected panelists. We are presenting our capstone project entitled An Intelligent Queuing Management System with Real-Time Wait-Time Analytics and Staff Allocation for MD57 Car Wash & Auto Detailing. Our project focuses on helping customers understand service progress and helping shop personnel organize bookings, queues, and employee assignments.
The application connects customer, staff, manager, and owner activities through one web-based system. Customers can book and monitor their vehicles. Staff can record attendance and handle assigned work. Managers oversee daily operations, while the owner manages restricted settings and employee access.
The system combines Random Forest time prediction with a weighted staff allocation method. Throughout this presentation, we will explain the operational problems, our objectives, the system workflow, and the evidence needed to evaluate its effectiveness.

Transition: To explain why we developed this system, let us begin with the background of the study.

Presenter note: Introduce your group members as shown on the slide. The title describes the project purpose; it does not by itself establish improved efficiency or prediction accuracy.


## Slide 2: Background of the Study

Our study focuses on three challenges: manual queuing and recordkeeping, limited waiting-time information, and difficulty matching available employees to changing customer demand.
With manual logbooks, personnel must repeatedly check which vehicles are waiting, being serviced, or ready for pickup. During busy periods, this makes it harder to follow multiple transactions and retrieve reliable service-duration records.
Customers also need more than a queue position. A short wash and a lengthy detailing service take different amounts of time, so the number of vehicles ahead does not fully explain when a customer can expect completion. Limited information creates uncertainty and can affect the customer experience.
Finally, demand changes throughout the day, while employees may already be working on other vehicles. These challenges motivated us to build digital records, visible queue progress, estimated turnaround times, and consistent staff allocation into one application.

Transition: The next slide connects these challenges to the specific functions we developed.

Presenter note: Use actual observations to support the description of MD57. Replace high walkout rates with customer uncertainty and possible walkouts unless the rate was measured. Do not assume static rosters alone caused congestion.


## Slide 3: Addressing Core Operational Bottlenecks

This slide connects each identified problem to a system response. For limited queue transparency, we provide a dashboard showing recorded service status and estimated time information. Customers can check progress without depending entirely on repeated verbal updates.
For employee assignment, the system identifies washers who are clocked in and not occupied by another ongoing or pending-confirmation job. It ranks eligible employees using service speed, punctuality, and customer ratings, then assigns the highest-ranked available washer when service starts.
For fragmented paper records, the application stores transactions, assignments, attendance, and operational timestamps in a central database. These records support reports and help management investigate delays.
Time prediction uses six inputs: same-service queue length, active staffing, rainfall, standard service duration, work ahead in the bay, and hour of day. This provides a more informed estimate than simply counting vehicles. The estimate remains subject to changing conditions.

Transition: These functions were developed to address our research objectives.

Presenter note: Correct Boftlenecks to Bottlenecks. Replace vehicle type and service depth as the full AI explanation with the six actual inputs. Daily job allocation and weekly staffing recommendations are separate processes.


## Slide 4: Objectives of the Study

Our general objective is to design, develop, and evaluate a queuing management system that supports service flow, customer expectations, and daily operational management.
The first area is operational analysis. We identify missing information in manual records and the conditions that contribute to delays. The system records service details and timestamps so waiting and service durations can be examined. Quantifying the original process still requires actual baseline observations.
The second area is staff allocation. We implemented MCDA using service speed at forty percent, punctuality at forty percent, and customer ratings at twenty percent. Availability checks prevent assignment to employees who are off duty or already busy.
The third area is tracking and analytics. Recorded status changes connect booking and arrival with service, completion review, and pickup. The final area is evaluation: checking functions and user experience, measuring performance and reliability, and comparing saved predictions with actual durations. Implemented features and demonstrated improvements must be reported separately.

Transition: The next slide explains the practical value expected from these functions.

Presenter note: Your paper has three specific objectives, while this slide organizes the work into four areas. Align the final slide with the approved manuscript. Its selected quality criteria are Functional Suitability, Usability, Performance Efficiency, and Reliability. Replace completion rate with punctuality when describing the implemented allocation.


## Slide 5: Problem to Solution to Operational Impact

This slide summarizes three intended operational benefits. First is transparency. Showing recorded progress and estimated completion information helps customers decide how to use their time and understand the status of their vehicles.
Second is consistent dispatch. Instead of choosing an employee without a defined basis, the system applies eligibility checks and weighted performance criteria. Workload is considered when scores tie. This supports an explainable assignment process, although it does not guarantee identical workloads for every employee.
Third is accessible operational information. Digital records allow management to review service durations, customer activity, staff performance, and service-value summaries without manually consolidating separate logbook entries.
These are the mechanisms through which the system aims to improve operations. To claim fewer walkouts, shorter waits, or reduced idle time, we would need to compare actual operational measurements before and after implementation.

Transition: We also defined boundaries so the project remains focused and realistic.

Presenter note: Describe impacts as intended benefits unless supported by measured results. Service-value or revenue summaries are not proof of collected payments, profit, or a complete accounting system.


## Slide 6: Scope and Limitations

The customer side includes service booking, vehicle and service selection, pricing information, and queue-status monitoring. The staff side includes attendance and assigned service workflows. Managers oversee queues, verify attendance and completed work, confirm pickups, and review operational reports. Owner access includes employee invitations and restricted administration.
The application is designed around the services, bays, schedules, and workflow configured for MD57. Applying it to another business would require configuration and possibly changes to its rules.
The deployed system requires internet access. Updates depend on the server and accurate actions by users; there are no physical sensors automatically detecting every vehicle movement. Estimates depend on available records, current queue conditions, and the prediction model. They are not guaranteed completion times.
The project does not provide a complete payment-processing, inventory, payroll, or accounting system. Hosting availability and database persistence also need attention when using a free hosting environment.

Transition: Within these boundaries, the system supports the following user groups.

Presenter note: Replace continuous local network connectivity with internet connectivity for the deployed application. Downloadable reports are available on request; do not imply scheduled report delivery unless configured and verified.


## Slide 7: Target Users and Beneficiaries

There are four account roles, even though this slide groups the beneficiaries into three categories. Customers use booking and tracking features to view their service progress and receive relevant notifications.
Staff use personal accounts to record attendance and work on assigned vehicles. Their recorded service activity and customer feedback contribute to the performance information used by management and allocation.
Managers supervise daily operations. They monitor queues and bays, validate attendance, review completed work, confirm pickups, and examine analytics. The owner has additional authority over employee access and restricted settings.
These role boundaries help keep operational tasks organized. A customer should only access their own permitted records, and an employee should only use functions allowed for their role. The expected benefits are clearer responsibilities and more accessible information, rather than guaranteed elimination of every delay or disagreement.

Transition: We will now explain the modules that connect these users.

Presenter note: Replace precise upfront wait times with estimated turnaround times. Replace eliminate assignment disputes with supports consistent assignment. A notification accepted by the email provider is not proof that it reached the inbox.


## Slide 8: Core System Modules

The booking module collects the vehicle and service details, checks available scheduling options, and records the reservation. The live queue module presents the recorded status of vehicles and the shop workload.
The staff module connects attendance, availability, and assignment. When a job starts, the allocation process chooses an eligible washer through weighted scoring. The completion workflow records the staff checklist and supports management review before the vehicle is marked ready.
The analytics and reporting module summarizes operational records, including waiting and service durations, staff activity, and customer information. CSV and PDF outputs support review outside the application where available.
Two decision methods support these modules. Random Forest predicts time from operational conditions. MCDA ranks eligible staff using defined weights. Scheduling, permission checks, and status transitions are controlled by application rules. Together, these components provide the complete workflow; not every system function is machine learning.

Transition: The following slide shows how these modules work through a single transaction.

Presenter note: This slide presents functional modules, rather than a complete technical architecture. If asked about architecture, explain the browser, Flask server, and SQLite database shown in the technology discussion.


## Slide 9: System Operational Flow

The process begins with a reservation or a walk-in entry. An online booking reserves a service opportunity; physical arrival is a separate operational event. The system records the vehicle and selected service, checks scheduling and bay rules, and provides time information.
The vehicle waits until the service can begin. At service start, the system checks eligible available washers and assigns the highest-ranked candidate. Its status then becomes in progress, and actual work timestamps are recorded.
When the washer finishes, the checklist and completion information support review. A job submitted for confirmation remains under review until management accepts it or sends it back for further work. Once accepted, the vehicle becomes ready for pickup and the customer can be notified.
Ready does not mean collected. Personnel separately confirm physical pickup, recording who confirmed it and when. The completed transaction remains in history and can support eligible feedback, loyalty records, reporting, and future prediction training.

Transition: Now that the workflow is clear, we can explain the technologies supporting it.

Presenter note: Expand the four-step diagram to include service work, completion review, and confirmed pickup. Sending a ready notification does not automatically archive the vehicle as picked up. Do not describe a QR arrival process; that feature is not implemented.


## Slide 10: Technology Stack

The frontend uses HTML, custom CSS, and JavaScript to present forms, dashboards, and interactive controls. Jinja templates allow the backend to render pages according to the signed-in user and their permissions. Chart.js supports visual reporting.
The backend uses Python and Flask. It handles authentication, booking validation, scheduling rules, staff allocation, status transitions, and API requests. Waitress serves the application in its deployed runtime.
The database is SQLite. Related tables store users, services, reservations, attendance, assignments, feedback, and other operational records. The backend reads and updates these records as authorized actions occur.
For prediction, scikit-learn provides the Random Forest Regressor with one hundred decision trees. MCDA uses a separate weighted scoring calculation. Google sign-in supports authentication where configured, and Brevo provides HTTPS email delivery requests. These external services require correct credentials and configuration; successful application deployment alone does not verify every integration.

Transition: The final technical question is how we evaluate whether these functions work as intended.

Presenter note: Remove Tailwind CSS from the slide. Name Flask and SQLite explicitly. The hosting arrangement is Render; an old Vercel address should not be presented as working without checking it. SQLite needs persistent storage or a suitable persistence strategy for durable deployed records.


## Slide 11: ISO/IEC 25010 Evaluation

This slide should explain both the evaluation method and the verified results. Functional tests check whether booking, access restrictions, attendance, assignment, completion review, pickup, and reporting behave as expected. User evaluation examines whether actual participants can understand and operate the application.
The selected software quality criteria in our manuscript are Functional Suitability, Usability, Performance Efficiency, and Reliability. Functional suitability is supported by documented workflow results. Usability requires actual participant responses. Performance efficiency requires measured response conditions, while reliability requires evidence of consistent behavior and appropriate handling of errors.
Prediction accuracy must be evaluated separately by comparing saved estimated durations with actual eligible completion durations. MAE expresses average absolute error in minutes; RMSE gives greater weight to larger errors. Both require a reported sample size and a clearly described evaluation dataset.
The current slide lists result categories without showing the results themselves. Until the underlying records are reconciled, we should describe the evaluation approach and avoid claiming that all objectives or accuracy targets have already been achieved.

Transition: We will close by summarizing the system contribution and inviting your questions.

Presenter note: Before defense, insert verified test totals, respondent counts, and criterion means. Remove POS and inventory testing claims. Reconcile the manuscript survey items totaling 24 and 25 with the stated 23 respondents. Distinguish four-point quality items from the separate five-point satisfaction item. Do not invent a 95% pass result, MAE, or RMSE. Simulated workdays do not establish real-world accuracy.


## Slide 12: Conclusion and Questions

To conclude, our project brings booking, queue tracking, employee availability, service completion, pickup confirmation, and reporting into one web application for MD57 Car Wash & Auto Detailing.
Its main contribution is combining operational records with estimated turnaround information and a consistent method of assigning available staff. Random Forest supports time prediction, while MCDA applies defined criteria to staff selection. The system records the transaction lifecycle so management can review what happened and who handled each stage.
The application provides the functions needed to support these goals. Claims about shorter waits, improved workload distribution, user satisfaction, or prediction accuracy must be supported by the corresponding evaluation evidence. Further improvement depends on reliable operational records, measured testing, and appropriate deployment configuration.
Thank you for listening to our presentation. We are ready to demonstrate the workflow and answer your questions about the design, implementation, and evaluation of the system.

Transition: End the prepared script here and proceed to questions or the system demonstration.

Presenter note: During questions, distinguish implemented behavior, intended benefit, and measured result. If a requested metric is not available, state that clearly. Prepare an actual booking-to-pickup demonstration and a local backup for connectivity problems.